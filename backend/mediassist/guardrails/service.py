"""Guardrail orchestration: deterministic rules first, then the OpenEvals judge.

Every check yields a ``GuardrailVerdict`` that is written to the structured
guardrail log and attached to the LangSmith trace. The user only ever sees
``SAFE_REFUSAL``; block reasons stay internal.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langsmith import traceable

from ..config import Settings
from ..observability import EventLogger, current_trace_id, record_langchain_usage, stage_timer
from ..rbac import allowed_collections
from ..schemas import Role
from . import rules
from .judge import GuardrailJudge, OpenEvalsGuardrailJudge
from .verdict import GuardrailVerdict, Stage, parse_verdict

SAFE_REFUSAL = (
    "I'm sorry, but I can't help with that request. Please ask a question about MediAssist "
    "policies, procedures or documents available to your role."
)


@dataclass
class GuardrailDecision:
    """All verdicts for one stage; the stage blocks if any single verdict blocks."""

    stage: Stage
    verdicts: list[GuardrailVerdict] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(verdict.blocked for verdict in self.verdicts)

    @property
    def blocking_verdict(self) -> GuardrailVerdict | None:
        return next((verdict for verdict in self.verdicts if verdict.blocked), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "blocked": self.blocked,
            "verdicts": [verdict.model_dump() for verdict in self.verdicts],
        }


class GuardrailService:
    """Runs the input and output guardrail layers around the chatbot."""

    def __init__(
        self,
        settings: Settings,
        events: EventLogger,
        judge: GuardrailJudge | None = None,
        judge_factory: Callable[[Settings], GuardrailJudge] = OpenEvalsGuardrailJudge,
        llm_enabled: bool | None = None,
    ):
        self.settings = settings
        self.events = events
        self._judge = judge
        self._judge_factory = judge_factory
        self.llm_enabled = settings.guardrail_llm_enabled if llm_enabled is None else llm_enabled

    def _get_judge(self) -> GuardrailJudge:
        if self._judge is None:
            self._judge = self._judge_factory(self.settings)
        return self._judge

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _run_rules(run: Callable[[], list[GuardrailVerdict]]) -> list[GuardrailVerdict]:
        """Run a rule set and spread its (sub-millisecond) latency across its verdicts."""

        start = time.perf_counter()
        verdicts = run()
        per_rule_ms = round((time.perf_counter() - start) * 1000 / max(len(verdicts), 1), 3)
        for verdict in verdicts:
            verdict.latency_ms = per_rule_ms
        return verdicts

    def _call_judge(self, stage: Stage, check: str, call: Callable[[GuardrailJudge], Any]) -> GuardrailVerdict:
        """Call the LLM judge; any exception or malformed payload fails closed."""

        from langchain_core.callbacks import get_usage_metadata_callback

        start = time.perf_counter()
        error = None
        raw: Any = None
        try:
            with get_usage_metadata_callback() as usage:
                raw = call(self._get_judge())
            record_langchain_usage("guardrail", usage.usage_metadata)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        verdict = parse_verdict(raw, stage=stage, check=check)
        if error is not None:
            verdict.reason = f"Fail-closed: judge call failed ({error[:300]})"
        verdict.latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return verdict

    def _log(self, decision: GuardrailDecision, request_id: str, role: Role, text: str) -> None:
        trace_id = current_trace_id()
        for verdict in decision.verdicts:
            self.events.log_guardrail(
                {
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "role": role,
                    "stage": verdict.stage,
                    "check": verdict.check,
                    "verdict": verdict.verdict,
                    "category": verdict.category,
                    "reason": verdict.reason,
                    "latency_ms": verdict.latency_ms,
                    "text_preview": text[:300],
                }
            )

    # ------------------------------------------------------------------- stages

    @traceable(name="MediBot: Input Guardrail", run_type="chain")
    def check_input(self, question: str, role: Role, request_id: str) -> GuardrailDecision:
        """Screen a request before the chatbot processes it."""

        with stage_timer("input_guardrail"):
            decision = GuardrailDecision("input", self._run_rules(lambda: rules.run_input_rules(question, role)))
            # Rules are cheap and decisive: only spend an LLM call when they pass.
            if not decision.blocked and self.llm_enabled:
                collections = allowed_collections(role)
                decision.verdicts.append(
                    self._call_judge(
                        "input", "openevals_input_judge",
                        lambda judge: judge.judge_input(question, role, collections),
                    )
                )
        self._log(decision, request_id, role, question)
        return decision

    @traceable(name="MediBot: Output Guardrail", run_type="chain")
    def check_output(
        self,
        question: str,
        answer: str,
        role: Role,
        request_id: str,
        contexts: list[str],
        source_collections: list[str],
        grounded: bool,
        use_llm: bool = True,
    ) -> GuardrailDecision:
        """Screen a response before it reaches the user.

        ``grounded`` marks document-RAG answers (figure checks apply). ``use_llm``
        is False for fixed system messages that contain no model output.
        """

        with stage_timer("output_guardrail"):
            decision = GuardrailDecision(
                "output",
                self._run_rules(lambda: rules.run_output_rules(answer, role, source_collections, contexts, grounded)),
            )
            if not decision.blocked and self.llm_enabled and use_llm:
                collections = allowed_collections(role)
                context = "\n\n".join(contexts)[:12000]
                decision.verdicts.append(
                    self._call_judge(
                        "output", "openevals_output_judge",
                        lambda judge: judge.judge_output(question, answer, context, role, collections),
                    )
                )
        self._log(decision, request_id, role, answer)
        return decision

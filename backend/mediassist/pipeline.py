"""The guarded MediBot request pipeline shared by the API and the evaluator.

Request path (every step is a LangSmith span and is recorded in the structured logs):

    input guardrail -> route (SQL RAG / RBAC denial / hybrid RAG)
        -> retrieval -> rerank -> generation -> output guardrail -> response
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from langsmith import traceable, uuid7

from .config import Settings
from .guardrails import SAFE_REFUSAL, GuardrailDecision, GuardrailService, GuardrailVerdict
from .observability import EventLogger, current_trace_id, request_scope, total_tokens
from .retrieval import HybridRetriever, RetrievedChunk
from .schemas import ChatResponse, Role, SourceCitation
from .sql_rag import is_analytical_question, sql_rag_chain

SQL_ROLES = {"billing_executive", "admin"}


def system_prompt(role: Role) -> str:
    """System prompt for document-grounded answers."""

    return (
        "You are MediBot, an internal MediAssist document summarization assistant. "
        f"The user is an authorized {role}; answer from the supplied context "
        "for that role's permitted collections. This includes equipment operation "
        "and preventive-maintenance questions for technicians. Do not refuse a "
        "permitted question merely because it concerns healthcare equipment or an "
        "internal document. This is document summarization, not diagnosis or personal "
        "medical advice. Use only the supplied context, give the relevant steps "
        "or schedule concisely, and say when the context does not contain the answer. "
        "Quote doses, thresholds and other figures exactly as written in the context; "
        "never add unit conversions, extra figures or general medical knowledge that the "
        "context does not state. "
        "Never reveal or infer content from restricted collections."
    )


def extractive_answer(question: str, chunks: list) -> str:
    """Return a grounded local answer when the configured model refuses or fails."""

    question_terms = {
        term
        for term in re.findall(r"[a-z0-9]+", question.lower())
        if len(term) > 2
    }
    preferred_lines: list[str] = []
    scored_lines: list[tuple[int, str]] = []
    seen: set[str] = set()
    for chunk in chunks:
        raw_lines = re.split(r"[\r\n]+", chunk.text)
        cleaned_lines = [re.sub(r"^[\u0000\x7f\s•]+", "", line).strip() for line in raw_lines]
        for index, raw_line in enumerate(raw_lines):
            line = re.sub(r"^[\u0000\x7f\s•]+", "", raw_line).strip()
            if len(line) < 12 or line.lower() in seen:
                continue
            seen.add(line.lower())
            lowered_line = line.lower()
            lowered_question = question.lower()
            is_block_anchor = (
                ("checklist" in lowered_question and "equipment checklist" in lowered_line)
                or ("maintenance" in lowered_question and "maintenance" in lowered_line)
                or ("stemi" in lowered_question and "stemi" in lowered_line)
            )
            if is_block_anchor:
                block_end = min(index + (12 if "checklist" in lowered_line else 6), len(cleaned_lines))
                preferred_lines.extend(item for item in cleaned_lines[index:block_end] if len(item) >= 4)
            line_terms = set(re.findall(r"[a-z0-9]+", line.lower()))
            overlap = len(question_terms & line_terms)
            if overlap or any(marker in line.lower() for marker in ("maintenance", "procedure", "protocol", "checklist", "stemi", "cvc")):
                scored_lines.append((overlap, line))
    scored_lines.sort(key=lambda item: item[0], reverse=True)
    selected: list[str] = []
    for line in preferred_lines + [line for _score, line in scored_lines]:
        if line.lower() not in {item.lower() for item in selected}:
            selected.append(line)
        if len(selected) >= 12:
            break
    if not selected:
        return "The retrieved documents do not contain enough detail to answer this question."
    return "Based on the retrieved MediAssist documents:\n\n" + "\n".join(f"- {line}" for line in selected)


def is_model_refusal(answer: str) -> bool:
    """Detect generic refusal text so it cannot hide retrieved answers."""

    lowered = answer.lower()
    return any(
        phrase in lowered
        for phrase in (
            "i’m sorry",
            "i'm sorry",
            "i cannot provide",
            "i can't provide",
            "don't have the information needed",
            "without the specific context",
        )
    )


@dataclass
class PipelineResult:
    """Everything the pipeline saw and decided for one request."""

    request_id: str
    question: str
    role: Role
    answer: str
    sources: list[SourceCitation]
    retrieval_type: Literal["hybrid_rag", "sql_rag", "none"]
    contexts: list[str] = field(default_factory=list)
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    raw_answer: str = ""
    llm_prompt: dict[str, str] | None = None
    input_verdicts: list[GuardrailVerdict] = field(default_factory=list)
    output_verdicts: list[GuardrailVerdict] = field(default_factory=list)
    blocked_stage: str | None = None
    block_category: str | None = None
    latency_ms: dict[str, float] = field(default_factory=dict)
    tokens: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None

    @property
    def guardrail(self) -> Literal["allowed", "blocked"]:
        return "blocked" if self.blocked_stage else "allowed"

    def to_response(self) -> ChatResponse:
        return ChatResponse(
            answer=self.answer,
            sources=self.sources,
            retrieval_type=self.retrieval_type,
            role=self.role,
            request_id=self.request_id,
            guardrail=self.guardrail,
        )

    def log_record(self, channel: str) -> dict[str, Any]:
        """The structured record written to ``requests.jsonl``."""

        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "channel": channel,
            "role": self.role,
            "question": self.question,
            "route": self.retrieval_type,
            "retrieved": self.retrieved,
            "llm_prompt": self.llm_prompt,
            "raw_answer": self.raw_answer,
            "final_answer": self.answer,
            "sources": [source.model_dump() for source in self.sources],
            "blocked": self.blocked_stage is not None,
            "blocked_stage": self.blocked_stage,
            "block_category": self.block_category,
            "guardrail_verdicts": [v.model_dump() for v in self.input_verdicts + self.output_verdicts],
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
        }


class ChatPipeline:
    """Guarded chat pipeline: input guardrail, RAG routing, output guardrail."""

    def __init__(
        self,
        retriever: HybridRetriever,
        llm: Any,
        guardrails: GuardrailService,
        events: EventLogger,
        settings: Settings,
    ):
        self.retriever = retriever
        self.llm = llm
        self.guardrails = guardrails
        self.events = events
        self.settings = settings

    def run(self, question: str, role: Role, channel: str = "api") -> PipelineResult:
        """Process one question end to end and log the full request record."""

        request_id = str(uuid7())
        with request_scope() as (timings, usage):
            start = time.perf_counter()
            result = self._run(
                question,
                role,
                request_id,
                langsmith_extra={
                    "run_id": request_id,
                    "metadata": {"request_id": request_id, "role": role, "channel": channel},
                    "tags": ["medibot", f"role:{role}", f"channel:{channel}"],
                },
            )
            result.latency_ms = {**timings, "total": round((time.perf_counter() - start) * 1000, 2)}
            result.tokens = {**total_tokens(usage), "by_component": usage}
        self.events.log_request(result.log_record(channel))
        return result

    @traceable(name="MediBot: Guarded Chat Request", run_type="chain")
    def _run(self, question: str, role: Role, request_id: str) -> PipelineResult:
        trace_id = current_trace_id()

        input_decision = self.guardrails.check_input(question, role, request_id)
        if input_decision.blocked:
            return self._blocked(question, role, request_id, trace_id, input_decision, [], retrieval_type="none")

        is_analytical = is_analytical_question(question, self.llm)
        if is_analytical and role not in SQL_ROLES:
            answer = (
                f"As a {role}, you do not have access to analytical database questions. "
                "I can only answer from your permitted document collections."
            )
            result = PipelineResult(request_id, question, role, answer, [], "hybrid_rag", raw_answer=answer, trace_id=trace_id)
            return self._screen_output(result, input_decision, grounded=False, use_llm=False)

        if is_analytical:
            return self._sql_route(question, role, request_id, trace_id, input_decision)
        return self._hybrid_route(question, role, request_id, trace_id, input_decision)

    # ------------------------------------------------------------------ routes

    def _sql_route(self, question, role, request_id, trace_id, input_decision) -> PipelineResult:
        database: Path = self.settings.database_file
        sources = [SourceCitation(source_document=database.name, section_title="SQL RAG result", collection="database")]
        try:
            answer, rows = sql_rag_chain(question, database, self.llm)
        except (ValueError, OSError):
            answer, rows = "I could not complete the database analysis. Please check the Groq configuration and database connection.", []
        contexts = [json.dumps(rows, default=str)] if rows else []
        result = PipelineResult(
            request_id, question, role, answer, sources, "sql_rag",
            contexts=contexts, retrieved=[{"sql_rows": rows[:50]}], raw_answer=answer, trace_id=trace_id,
        )
        return self._screen_output(result, input_decision, grounded=False, use_llm=bool(rows))

    def _hybrid_route(self, question, role, request_id, trace_id, input_decision) -> PipelineResult:
        chunks: list[RetrievedChunk] = self.retriever.retrieve(question, role)
        context = "\n\n".join(f"[{chunk.source_document} | {chunk.section_title}]\n{chunk.text}" for chunk in chunks)
        prompt = {"system": system_prompt(role), "user": f"Question: {question}\nContext:\n{context}"}
        raw_answer = self.llm.complete(prompt["system"], prompt["user"])
        answer = extractive_answer(question, chunks) if is_model_refusal(raw_answer) else raw_answer
        result = PipelineResult(
            request_id, question, role, answer, [chunk.citation() for chunk in chunks], "hybrid_rag",
            contexts=[chunk.text for chunk in chunks],
            retrieved=[
                {
                    "rank": rank,
                    "source_document": chunk.source_document,
                    "section_title": chunk.section_title,
                    "collection": chunk.collection,
                    "score": chunk.score,
                    "text_preview": chunk.text[:400],
                }
                for rank, chunk in enumerate(chunks, start=1)
            ],
            raw_answer=raw_answer,
            llm_prompt=prompt,
            trace_id=trace_id,
        )
        return self._screen_output(result, input_decision, grounded=True, use_llm=True)

    # ----------------------------------------------------------------- helpers

    def _screen_output(self, result: PipelineResult, input_decision: GuardrailDecision, grounded: bool, use_llm: bool) -> PipelineResult:
        output_decision = self.guardrails.check_output(
            result.question,
            result.answer,
            result.role,
            result.request_id,
            contexts=result.contexts,
            source_collections=[source.collection for source in result.sources],
            grounded=grounded,
            use_llm=use_llm,
        )
        result.input_verdicts = input_decision.verdicts
        result.output_verdicts = output_decision.verdicts
        if output_decision.blocked:
            verdict = output_decision.blocking_verdict
            result.answer = SAFE_REFUSAL
            result.sources = []
            result.blocked_stage = "output"
            result.block_category = verdict.category if verdict else "unknown"
        return result

    @staticmethod
    def _blocked(question, role, request_id, trace_id, decision: GuardrailDecision, sources, retrieval_type) -> PipelineResult:
        verdict = decision.blocking_verdict
        return PipelineResult(
            request_id, question, role, SAFE_REFUSAL, sources, retrieval_type,
            input_verdicts=decision.verdicts,
            blocked_stage=decision.stage,
            block_category=verdict.category if verdict else "unknown",
            trace_id=trace_id,
        )

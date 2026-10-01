"""Structured guardrail verdicts with fail-closed parsing.

A verdict is never inferred from free text. Every check - rule-based or LLM - must
produce ``{"verdict": "allow" | "block", "category": ..., "reason": ...}``. Anything
else (missing field, unknown value, invalid JSON, ``None``, an exception) becomes a
``block`` with category ``malformed_verdict``.
"""

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

Stage = Literal["input", "output"]
VerdictValue = Literal["allow", "block"]

MALFORMED_CATEGORY = "malformed_verdict"

# JSON schema handed to the OpenEvals judge so the LLM returns a structured verdict.
VERDICT_JSON_SCHEMA: dict[str, Any] = {
    "title": "guardrail_verdict",
    "description": "Structured allow/block decision from the MediBot guardrail.",
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["allow", "block"]},
        "category": {"type": "string", "description": "Short snake_case policy category, or 'none' when allowed."},
        "reason": {"type": "string", "description": "One-sentence internal justification (never shown to users)."},
    },
    "required": ["verdict", "category", "reason"],
    "additionalProperties": False,
}


class GuardrailVerdict(BaseModel):
    """One structured decision produced by one guardrail check."""

    stage: Stage
    check: str
    verdict: VerdictValue
    category: str = "none"
    reason: str = ""
    latency_ms: float = Field(default=0.0, ge=0)

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"


def allow(stage: Stage, check: str, reason: str = "No policy violation detected.") -> GuardrailVerdict:
    return GuardrailVerdict(stage=stage, check=check, verdict="allow", category="none", reason=reason)


def block(stage: Stage, check: str, category: str, reason: str) -> GuardrailVerdict:
    return GuardrailVerdict(stage=stage, check=check, verdict="block", category=category, reason=reason)


def parse_verdict(raw: Any, *, stage: Stage, check: str) -> GuardrailVerdict:
    """Validate a raw verdict payload, failing closed on anything unexpected."""

    try:
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if not isinstance(raw, Mapping):
            raise ValueError(f"verdict payload is {type(raw).__name__}, expected an object")
        value = raw.get("verdict")
        if not isinstance(value, str) or value.strip().lower() not in ("allow", "block"):
            raise ValueError(f"missing or invalid 'verdict' field: {value!r}")
        value = value.strip().lower()
        category = str(raw.get("category") or ("none" if value == "allow" else "policy_violation"))
        reason = str(raw.get("reason") or "")
        return GuardrailVerdict(stage=stage, check=check, verdict=value, category=category, reason=reason)
    except Exception as exc:  # fail closed on every parsing problem
        return block(stage, check, MALFORMED_CATEGORY, f"Fail-closed: {exc}")

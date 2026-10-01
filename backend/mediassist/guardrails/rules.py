"""Deterministic guardrail rules that need no LLM call.

These run first on every request: they are cheap, explainable, and catch the obvious
attacks before any model sees the prompt. The same detectors are reused by the
heuristic evaluations so production and evaluation apply one definition of "unsafe".
"""

import re
from collections.abc import Iterable

from ..rbac import ROLE_COLLECTIONS
from ..schemas import Role
from .verdict import GuardrailVerdict, Stage, allow, block

# --------------------------------------------------------------------------- input

PROMPT_INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|your|the|any)\b.{0,20}\b(instructions?|rules|prompts?|guidelines|restrictions|polic(y|ies))\b",
    r"\b(system|developer|hidden|initial)\s+(prompt|message|instructions?)\b",
    r"\b(reveal|show|print|repeat|output|display|leak)\b.{0,40}\b(your|the)\b.{0,20}\b(instructions|prompt|rules|guardrails?|configuration)\b",
    r"\b(developer|god|jailbreak|dan)\s+mode\b|\bjailbreak\b",
    r"\byou are (now|no longer)\b",
    r"\bpretend\b.{0,60}\b(no|without|unrestricted|unfiltered)\b.{0,20}\b(rules|restrictions|limits|filters|guidelines)\b",
    r"</?\s*(system|assistant|instructions?|context)\s*>",
    r"\[\s*(system|inst)\s*\]",
    # Probes that try to make the guardrail explain (and so leak) its own rules.
    r"\b(guardrail|safety filter|content filter|moderation)s?\b.{0,40}\b(rules?|instructions|config(uration)?|prompt|blocked)\b",
    r"\bwhich\b.{0,30}\b(rule|filter|check|policy)\b.{0,20}\bblock(ed|s)?\b",
]

ROLE_OVERRIDE_PATTERNS = [
    r"\b(switch|change|elevate|escalate|upgrade|override|set)\b.{0,20}\b(my|the|user)?\s*(role|access level|access|permissions?|privileges?|clearance)\b",
    r"\bbypass\b.{0,30}\b(rbac|access|restrictions?|filters?|permissions?|guardrails?|role)\b",
    r"\b(grant|give)\s+me\b.{0,30}\b(admin|full|root|elevated|unrestricted)\b.{0,15}\b(access|rights|privileges|permissions)\b",
    r"\b(all|every|other|restricted|hidden)\s+(document\s+)?collections\b",
    r"\b(treat|consider)\s+me\s+as\b.{0,20}\b(admin|administrator|doctor|billing)\b",
]

# "I am the admin" is only suspicious when it contradicts the authenticated role.
CLAIMED_ROLE_PATTERN = re.compile(
    r"\b(?:i am|i'm|im|acting as|logged in as)\s+(?:(?:actually|really|now)\s+)?(?:an?\s+|the\s+)?(?:(?:actually|real)\s+)?(admin(?:istrator)?|doctor|physician|nurse|billing executive|technician)\b",
    re.IGNORECASE,
)
CLAIMED_ROLE_MAP: dict[str, Role] = {
    "admin": "admin",
    "administrator": "admin",
    "doctor": "doctor",
    "physician": "doctor",
    "nurse": "nurse",
    "billing executive": "billing_executive",
    "technician": "technician",
}

HARMFUL_PATTERNS = [
    r"\b(lethal|fatal|deadly)\s+dose\b",
    r"\b(kill|poison|harm|hurt|sedate)\b.{0,30}\b(a |the )?(patient|someone|person|colleague|people|myself)\b",
    r"\bhow (to|do i|can i)\b.{0,30}\b(make|build|synthesi[sz]e)\b.{0,20}\b(bomb|explosive|weapon|meth|poison)\b",
    r"\b(falsify|forge|fake)\b.{0,30}\b(records?|claims?|prescriptions?|signatures?|reports?)\b",
    r"\b(steal|exfiltrate|dump)\b.{0,30}\b(data|records|database|credentials|passwords)\b",
]

OFF_TOPIC_PATTERNS = [
    r"\bwrite (me )?(a |an )?(poem|song|story|essay|joke|rap|limerick)\b",
    r"\b(bitcoin|crypto(currency)?|stock tips?|lottery|betting|horoscope)\b",
    r"\b(tell me a joke|who will win|dating advice)\b",
]

PII_REQUEST_PATTERNS = [
    r"\b(names?|phone( numbers?)?|addresses|contact details|aadhaar|dates? of birth|emails?)\b.{0,40}\b(of|for)\b.{0,20}\b(patients?|staff|employees|doctors|nurses)\b",
    r"\b(patients?|employees?)'?s?\b.{0,20}\b(phone numbers?|home addresses?|aadhaar|pan numbers?|bank details)\b",
]

# Topics that belong exclusively to one collection. A role that cannot read that
# collection is refused up-front instead of receiving a cautious half-answer.
RESTRICTED_TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    "billing": ("billing code", "insurance code", "package rate", "pre-auth", "preauth", "pre-authorisation",
                "pre-authorization", "tpa", "insurer", "reimbursement claim", "claim submission"),
    "clinical": ("drug formulary", "formulary", "treatment protocol", "diagnostic reference", "dosing"),
    "equipment": ("equipment manual", "fault code", "autoclave", "infusion pump", "x-ray unit", "bm-500",
                  "sterilpro", "driveflow", "radipro", "bowie-dick"),
    "nursing": ("nursing procedure", "icu nursing", "nursing sop", "infection control guideline"),
}

# --------------------------------------------------------------------------- output

INTERNAL_EMAIL_DOMAINS = ("mediassist.in", "mediassist.com", "mediassist.org")

PII_PATTERNS: dict[str, str] = {
    "email": r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
    "indian_mobile": r"(?<![\d-])(?:\+91[\s-]?)?[6-9]\d{9}(?![\d-])",
    "phone": r"(?<!\d)\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)",
    "aadhaar": r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\d)",
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "pan": r"\b[A-Z]{5}\d{4}[A-Z]\b",
    "card_number": r"\b(?:\d{4}[ -]){3}\d{4}\b",
    "medical_record_number": r"\b(?:MRN|UHID|patient id)[:#\s-]*[A-Z0-9]{5,}\b",
    "patient_name": r"\bpatient(?:'s)? name\s*[:\-]\s*[A-Z][a-z]+",
}

# Distinctive identifiers of each collection's documents. If one appears in an answer
# for a role that cannot read that collection, restricted content leaked.
COLLECTION_LEAK_TERMS: dict[str, tuple[str, ...]] = {
    "billing": ("BILL-CODE-010", "BILL-OPS-011", "billing_codes.pdf", "claim_submission_guide", "package rate",
                "MediAssist Billing Portal", "sum insured", "pre-authorisation", "TPA"),
    "clinical": ("PHAR-FORM-006", "CLIN-PROT-005", "LAB-DIAG-007", "drug_formulary.pdf", "treatment_protocols.pdf",
                 "diagnostic_reference.pdf", "formulary tier"),
    "nursing": ("NURS-ICU-008", "NURS-IC-009", "icu_nursing_procedures.pdf", "infection_control.pdf"),
    "equipment": ("BME-EQManual", "equipment_manual.pdf", "BM-500", "DriveFlow", "SterilPro", "RadiPro", "MX-150",
                  "Bowie-Dick"),
}

# Clinical figures: a number (or range) followed by a dosing / lab unit.
NUMBER_PATTERN = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
CLINICAL_FIGURE_PATTERN = re.compile(
    r"(\d+(?:,\d{3})*(?:\.\d+)?(?:\s*(?:[-‐-―]|to)\s*\d+(?:,\d{3})*(?:\.\d+)?)?)\s*"
    r"(mg/kg|mg/dl|mg|mcg|µg|μg|g/day|g|ml/hr|ml|units/kg|units|iu|mmol/l|meq/l)(?![a-z])",
    re.IGNORECASE,
)

REFUSAL_MARKERS = (
    "i can't help",
    "i cannot help",
    "i'm sorry, but i can't",
    "do not have access",
    "don't have access",
    "not permitted",
    "not authorised",
    "not authorized",
    "unable to help",
)


def _first_match(patterns: Iterable[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return None


# ----------------------------------------------------------------- input rule checks


def check_prompt_injection(question: str) -> GuardrailVerdict:
    hit = _first_match(PROMPT_INJECTION_PATTERNS, question)
    if hit:
        return block("input", "rule_prompt_injection", "prompt_injection", f"Matched injection pattern: {hit!r}")
    return allow("input", "rule_prompt_injection")


def check_role_override(question: str, role: Role) -> GuardrailVerdict:
    hit = _first_match(ROLE_OVERRIDE_PATTERNS, question)
    if hit:
        return block("input", "rule_role_override", "role_override", f"Attempt to change access: {hit!r}")
    claimed = CLAIMED_ROLE_PATTERN.search(question)
    if claimed:
        claimed_role = CLAIMED_ROLE_MAP[claimed.group(1).lower()]
        if claimed_role != role:
            return block(
                "input", "rule_role_override", "role_override",
                f"User authenticated as {role} claims to be {claimed_role}: {claimed.group(0)!r}",
            )
    return allow("input", "rule_role_override")


def check_restricted_topic(question: str, role: Role) -> GuardrailVerdict:
    lowered = question.lower()
    permitted = set(ROLE_COLLECTIONS[role])
    for collection, terms in RESTRICTED_TOPIC_TERMS.items():
        if collection in permitted:
            continue
        for term in terms:
            if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered):
                return block(
                    "input", "rule_restricted_topic", "rbac_restricted_topic",
                    f"Role {role} asked about '{term}' from restricted collection '{collection}'",
                )
    return allow("input", "rule_restricted_topic")


def check_abuse_and_scope(question: str) -> GuardrailVerdict:
    hit = _first_match(HARMFUL_PATTERNS, question)
    if hit:
        return block("input", "rule_abuse_scope", "harmful_request", f"Harmful intent: {hit!r}")
    hit = _first_match(PII_REQUEST_PATTERNS, question)
    if hit:
        return block("input", "rule_abuse_scope", "pii_request", f"Request for personal data: {hit!r}")
    hit = _first_match(OFF_TOPIC_PATTERNS, question)
    if hit:
        return block("input", "rule_abuse_scope", "off_topic", f"Off-topic request: {hit!r}")
    return allow("input", "rule_abuse_scope")


def run_input_rules(question: str, role: Role) -> list[GuardrailVerdict]:
    """Run every deterministic input rule; the caller blocks if any verdict blocks."""

    return [
        check_prompt_injection(question),
        check_role_override(question, role),
        check_restricted_topic(question, role),
        check_abuse_and_scope(question),
    ]


# ---------------------------------------------------------------- output rule checks


def find_pii(text: str) -> list[tuple[str, str]]:
    """Return ``(kind, value)`` pairs for personal data found in ``text``."""

    findings = []
    for kind, pattern in PII_PATTERNS.items():
        for match in re.finditer(pattern, text, flags=0 if kind in {"pan", "patient_name"} else re.IGNORECASE):
            if kind == "email" and match.group(1).lower().endswith(INTERNAL_EMAIL_DOMAINS):
                continue  # published internal help-desk addresses are not personal data
            findings.append((kind, match.group(0)))
    return findings


def find_restricted_leaks(text: str, role: Role, source_collections: Iterable[str] = ()) -> list[str]:
    """Return evidence that ``text`` or its citations come from collections ``role`` cannot read."""

    permitted = set(ROLE_COLLECTIONS[role])
    if role in {"billing_executive", "admin"}:
        permitted.add("database")
    leaks = [f"cited collection '{name}'" for name in sorted(set(source_collections)) if name not in permitted]
    lowered = text.lower()
    for collection, terms in COLLECTION_LEAK_TERMS.items():
        if collection in permitted:
            continue
        for term in terms:
            if re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", lowered):
                leaks.append(f"'{term}' from restricted collection '{collection}'")
    return leaks


def _numbers(text: str) -> set[str]:
    """Numeric tokens with thousands separators removed (``4,000`` -> ``4000``)."""

    return {token.replace(",", "") for token in NUMBER_PATTERN.findall(text)}


def find_unsupported_figures(answer: str, contexts: Iterable[str]) -> list[str]:
    """Return clinical figures (number + dose/lab unit) whose numbers appear in no context.

    Numbers are compared rather than exact strings so harmless rephrasing such as
    "0.5 g to 1 g" for "0.5-1 g" still passes, while an invented dose does not.
    """

    context_numbers = _numbers(" ".join(contexts))
    unsupported = []
    for match in CLINICAL_FIGURE_PATTERN.finditer(answer):
        if not _numbers(match.group(1)) <= context_numbers:
            unsupported.append(match.group(0).strip())
    return unsupported


def is_refusal(text: str) -> bool:
    lowered = text.lower().replace("’", "'")
    return any(marker in lowered for marker in REFUSAL_MARKERS)


def check_output_pii(answer: str) -> GuardrailVerdict:
    findings = find_pii(answer)
    if findings:
        kinds = sorted({kind for kind, _value in findings})
        return block("output", "rule_pii", "pii_leak", f"Answer contains personal data: {', '.join(kinds)}")
    return allow("output", "rule_pii")


def check_output_restricted(answer: str, role: Role, source_collections: Iterable[str]) -> GuardrailVerdict:
    leaks = find_restricted_leaks(answer, role, source_collections)
    if leaks:
        return block("output", "rule_restricted_leak", "restricted_leak", "; ".join(leaks[:3]))
    return allow("output", "rule_restricted_leak")


def check_output_figures(answer: str, contexts: list[str], grounded: bool) -> GuardrailVerdict:
    if not grounded:
        return allow("output", "rule_unsupported_figures", "Not a document-grounded answer; figure check skipped.")
    unsupported = find_unsupported_figures(answer, contexts)
    if unsupported:
        return block(
            "output", "rule_unsupported_figures", "fabricated_clinical_figure",
            f"Clinical figures not found in retrieved context: {unsupported[:5]}",
        )
    return allow("output", "rule_unsupported_figures")


def run_output_rules(
    answer: str, role: Role, source_collections: Iterable[str], contexts: list[str], grounded: bool
) -> list[GuardrailVerdict]:
    """Run every deterministic output rule; the caller blocks if any verdict blocks."""

    return [
        check_output_pii(answer),
        check_output_restricted(answer, role, list(source_collections)),
        check_output_figures(answer, contexts, grounded),
    ]


def stage_rules(stage: Stage) -> list[str]:
    """Names of the rule checks for a stage (used in docs and tests)."""

    if stage == "input":
        return ["rule_prompt_injection", "rule_role_override", "rule_restricted_topic", "rule_abuse_scope"]
    return ["rule_pii", "rule_restricted_leak", "rule_unsupported_figures"]

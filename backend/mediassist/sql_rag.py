"""Read-only SQL RAG chain for analytical questions."""

import re
import sqlite3
from pathlib import Path

from langsmith import traceable

from .llm import GroqLLM
from .observability import stage_timer

ALLOWED_TABLES = {"claims", "maintenance_tickets"}


@traceable(name="MediBot: Route Classifier", run_type="chain")
def is_analytical_question(question: str, llm: GroqLLM | None = None) -> bool:
    """Classify database questions using intent cues, with an LLM fallback.

    The local rules recognize natural-language analytical forms such as
    ``number of``, ``breakdown``, and ``distribution``. If a question is
    ambiguous (it has a database or aggregate cue but no clear entity/form
    pair), Groq classifies its intent instead of relying on one exact keyword
    such as ``count``. Questions with no such cue are document questions.
    """

    lowered = question.lower().strip()
    has_database_entity = any(
        entity in lowered for entity in ("claim", "claims", "ticket", "tickets", "maintenance")
    )
    analytical_forms = (
        "how many",
        "number of",
        "count of",
        "count",
        "total",
        "average",
        "mean",
        "sum",
        "percentage",
        "breakdown",
        "distribution",
    )
    if has_database_entity and any(form in lowered for form in analytical_forms):
        return True
    if has_database_entity and any(status in lowered for status in ("approved", "pending", "rejected", "open", "resolved", "escalated")):
        return True
    # Only questions with some database or aggregate cue are worth an LLM call.
    # Plain document questions ("What PPE is needed...?") must never be routed to
    # SQL RAG - for non-billing roles that would wrongly refuse them.
    database_cues = analytical_forms + (
        "insurer", "department", "highest", "lowest", "most", "least", "slowest",
        "fastest", "trend", "per month", "per year", "amount",
    )
    if llm is None or not any(cue in lowered for cue in database_cues):
        return False
    classification = llm.complete(
        "Classify the question as DATABASE or DOCUMENT. DATABASE means it needs counts, "
        "totals, averages or rankings computed over the claims or maintenance_tickets "
        "tables. DOCUMENT means it asks about policies, procedures, protocols, doses or "
        "manuals. Return only one label.",
        question,
    ).strip().upper()
    return classification == "DATABASE"


def _deterministic_sql(question: str) -> str | None:
    """Handle common count intents without depending on model interpretation."""

    lowered = question.lower()
    if "claim" in lowered and (
        "not approved" in lowered
        or "unapproved" in lowered
        or "non-approved" in lowered
        or "non approved" in lowered
    ):
        return "SELECT COUNT(*) AS not_approved_claims FROM claims WHERE lower(status) <> 'approved'"
    if "claim" in lowered and "approved" in lowered:
        return "SELECT COUNT(*) AS approved_claims FROM claims WHERE lower(status) = 'approved'"
    if "ticket" in lowered and any(term in lowered for term in ("open", "active", "unresolved")):
        return "SELECT COUNT(*) AS open_tickets FROM maintenance_tickets WHERE lower(status) NOT IN ('resolved', 'closed')"
    return None


def _count_answer(question: str, rows: list[dict]) -> str | None:
    """Format a scalar count directly so a second LLM refusal cannot erase it."""

    if len(rows) != 1 or len(rows[0]) != 1:
        return None
    value = next(iter(rows[0].values()))
    if not isinstance(value, int):
        return None
    if "claim" in question.lower() and any(
        phrase in question.lower()
        for phrase in ("not approved", "unapproved", "non-approved", "non approved")
    ):
        return f"There are {value} billing claims that are not approved."
    if "claim" in question.lower() and "approved" in question.lower():
        return f"There are {value} approved billing claims."
    if "ticket" in question.lower():
        return f"There are {value} open maintenance tickets."
    return f"The count is {value}."


def clean_sql(raw_sql: str) -> str:
    """Extract one safe SELECT statement from common LLM formatting noise."""

    statement = raw_sql.replace("```sql", "").replace("```", "").strip()
    match = re.search(r"\bSELECT\b[\s\S]*", statement, flags=re.IGNORECASE)
    if not match:
        raise ValueError("The model did not return a SELECT statement.")
    statement = match.group(0).split(";", 1)[0].strip()
    if not re.match(r"^SELECT\b", statement, flags=re.IGNORECASE):
        raise ValueError("Only SELECT statements are allowed.")
    tables = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-zA-Z_]+)", statement, flags=re.IGNORECASE))
    if not tables or not tables.issubset(ALLOWED_TABLES) or re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|PRAGMA)\b", statement, re.I):
        raise ValueError("The SQL statement references an unsupported table or operation.")
    return statement


@traceable(name="MediBot: SQL RAG", run_type="chain")
def sql_rag_chain(question: str, database_path: Path, llm: GroqLLM) -> tuple[str, list[dict]]:
    """Generate SQL, execute it read-only, and summarize the returned rows."""

    schema = "claims(claim_id, department, diagnosis_code, claimed_amount, approved_amount, status, submitted_date); maintenance_tickets(ticket_id, equipment_name, category, issue_type, status, raised_date)"
    sql = _deterministic_sql(question) or clean_sql(llm.generate_sql(question, schema))
    with stage_timer("retrieval"), sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(sql).fetchall()]
    return _count_answer(question, rows) or llm.answer_from_rows(question, rows), rows

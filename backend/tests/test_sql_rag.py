"""Tests for analytical intent routing, SQL safety, and the SQL RAG chain."""

import sqlite3

import pytest

from mediassist.config import get_settings
from mediassist.sql_rag import _count_answer, _deterministic_sql, clean_sql, is_analytical_question, sql_rag_chain


@pytest.fixture
def database_path():
    return get_settings().database_file


def _scalar(database_path, sql: str) -> int:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(sql).fetchone()[0]


@pytest.mark.parametrize(
    "question",
    [
        "How many claims are approved?",
        "What is the total claimed amount?",
        "Give me a breakdown of maintenance tickets by category",
        "Show the distribution of claims by department",
        "List pending claims",
        "Which tickets are escalated?",
    ],
)
def test_analytical_questions_are_detected_by_rules(question):
    assert is_analytical_question(question) is True


@pytest.mark.parametrize(
    "question",
    ["What is the STEMI protocol?", "How do I clean the ventilator?", "What is the leave policy?"],
)
def test_document_questions_are_not_analytical(question):
    assert is_analytical_question(question) is False


def test_ambiguous_questions_fall_back_to_llm_classification(fake_llm):
    fake_llm.classification = " database\n"

    assert is_analytical_question("Which insurer pays slowest?", fake_llm) is True
    assert fake_llm.calls[-1][0].startswith("Classify")


def test_llm_document_classification_is_not_analytical(fake_llm):
    assert is_analytical_question("Which insurer pays slowest?", fake_llm) is False


@pytest.mark.parametrize(
    "raw",
    [
        "SELECT * FROM claims",
        "```sql\nSELECT * FROM claims;\n```",
        "Here is the query: SELECT COUNT(*) FROM maintenance_tickets; DROP TABLE claims",
    ],
)
def test_clean_sql_extracts_first_select(raw):
    statement = clean_sql(raw)

    assert statement.upper().startswith("SELECT")
    assert ";" not in statement
    assert "DROP" not in statement


def test_clean_sql_allows_join_between_permitted_tables():
    sql = "SELECT * FROM claims c JOIN maintenance_tickets t ON 1=1"

    assert clean_sql(sql) == sql


@pytest.mark.parametrize(
    "raw",
    [
        "DROP TABLE claims",
        "I cannot answer that.",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM claims JOIN users ON 1=1",
        "SELECT 1",
        "SELECT * FROM claims WHERE status IN (SELECT 1) AND PRAGMA",
    ],
)
def test_clean_sql_rejects_unsafe_statements(raw):
    with pytest.raises(ValueError):
        clean_sql(raw)


@pytest.mark.parametrize(
    "question,expected_fragment",
    [
        ("How many claims are not approved?", "<> 'approved'"),
        ("How many unapproved claims?", "<> 'approved'"),
        ("How many claims are approved?", "= 'approved'"),
        ("How many open tickets?", "NOT IN ('resolved', 'closed')"),
    ],
)
def test_deterministic_sql_for_common_counts(question, expected_fragment):
    assert expected_fragment in _deterministic_sql(question)


def test_deterministic_sql_returns_none_for_other_questions():
    assert _deterministic_sql("Average claimed amount by department") is None


def test_count_answer_only_formats_single_integer():
    assert _count_answer("How many open tickets?", [{"n": 3}]) == "There are 3 open maintenance tickets."
    assert _count_answer("How many?", [{"n": 3}]) == "The count is 3."
    assert _count_answer("How many?", [{"n": 1.5}]) is None
    assert _count_answer("How many?", [{"a": 1, "b": 2}]) is None
    assert _count_answer("How many?", []) is None


def test_sql_rag_chain_answers_deterministic_counts(database_path, fake_llm):
    expected = _scalar(database_path, "SELECT COUNT(*) FROM claims WHERE lower(status) <> 'approved'")

    answer, rows = sql_rag_chain("How many claims are not approved?", database_path, fake_llm)

    assert answer == f"There are {expected} billing claims that are not approved."
    assert rows == [{"not_approved_claims": expected}]


def test_sql_rag_chain_counts_open_tickets(database_path, fake_llm):
    expected = _scalar(
        database_path, "SELECT COUNT(*) FROM maintenance_tickets WHERE lower(status) NOT IN ('resolved', 'closed')"
    )

    answer, _rows = sql_rag_chain("How many open tickets are there?", database_path, fake_llm)

    assert answer == f"There are {expected} open maintenance tickets."


def test_sql_rag_chain_uses_llm_sql_and_summary(database_path, fake_llm):
    fake_llm.sql = "```sql\nSELECT department, COUNT(*) AS n FROM claims GROUP BY department ORDER BY department;\n```"

    answer, rows = sql_rag_chain("Breakdown of claims by department", database_path, fake_llm)

    assert rows and set(rows[0]) == {"department", "n"}
    assert answer == f"Rows: {rows}"


def test_sql_rag_chain_rejects_unsafe_llm_sql(database_path, fake_llm):
    fake_llm.sql = "DELETE FROM claims"

    with pytest.raises(ValueError):
        sql_rag_chain("Average claimed amount by department", database_path, fake_llm)


@pytest.mark.parametrize(
    "question",
    [
        "What is the maximum daily dose of paracetamol?",
        "What PPE is required for an aerosol-generating procedure?",
        "What is the immediate management of NSTEMI in the first 60 minutes?",
    ],
)
def test_plain_document_questions_never_reach_llm_classifier(fake_llm, question):
    fake_llm.classification = "DATABASE"  # even a misbehaving classifier cannot misroute these

    assert is_analytical_question(question, fake_llm) is False
    assert fake_llm.calls == []

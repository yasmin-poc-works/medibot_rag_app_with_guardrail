"""Shared fixtures that keep the test suite offline and deterministic.

``mediassist.main`` ingests every source document and builds the hybrid index
at import time, ``GroqLLM`` calls the network, and the guardrail layer calls an
OpenEvals judge. The fixtures below replace all three with small in-memory
stand-ins so tests never download models or need a Groq or LangSmith key.
"""

import os
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Environment must be set before ``mediassist.config`` is imported because
# ``load_dotenv`` never overrides variables that already exist.
os.environ.update(
    {
        "GROQ_API_KEY": "",
        "GROQ_MODEL": "",
        "QDRANT_URL": "http://localhost:6333",
        "QDRANT_COLLECTION": "mediassist_test",
        "QDRANT_PATH": "",
        "DATABASE_PATH": "mediassist_db/db/mediassist.db",
        "FRONTEND_ORIGIN": "http://localhost:3000",
        "JUDGE_MODEL": "test-judge-model",
        "GUARDRAIL_LLM_ENABLED": "true",
        "LOG_DIR": tempfile.mkdtemp(prefix="medibot-test-logs-"),
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
    }
)

SAMPLE_CHUNKS: list[dict] = [
    {
        "text": "Central venous catheter (CVC) insertion steps: perform hand hygiene, use full barrier precautions.",
        "source_document": "icu_nursing_procedures.pdf",
        "section_title": "CVC Care",
        "collection": "nursing",
        "chunk_type": "text",
        "chunk_index": 0,
    },
    {
        "text": "STEMI protocol: administer aspirin and activate the cath lab within 90 minutes.",
        "source_document": "treatment_protocols.pdf",
        "section_title": "Cardiac Emergencies",
        "collection": "clinical",
        "chunk_type": "text",
        "chunk_index": 0,
    },
    {
        "text": "Claim submission guide: attach the diagnosis code and submit claims within 30 days.",
        "source_document": "claim_submission_guide.md",
        "section_title": "Submitting Claims",
        "collection": "billing",
        "chunk_type": "text",
        "chunk_index": 0,
    },
    {
        "text": "Ventilator preventive maintenance schedule: inspect filters monthly and calibrate sensors quarterly.",
        "source_document": "equipment_manual.pdf",
        "section_title": "Preventive Maintenance",
        "collection": "equipment",
        "chunk_type": "text",
        "chunk_index": 0,
    },
    {
        "text": "Annual leave policy: staff accrue 24 days of paid leave per year.",
        "source_document": "leave_policy.pdf",
        "section_title": "Annual Leave",
        "collection": "general",
        "chunk_type": "text",
        "chunk_index": 0,
    },
]


class FakeLLM:
    """Stand-in for ``GroqLLM`` with scriptable responses."""

    def __init__(self, answer: str = "Grounded answer.", classification: str = "DOCUMENT", sql: str = ""):
        self.answer = answer
        self.classification = classification
        self.sql = sql
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if system.startswith("Classify"):
            return self.classification
        return self.answer

    def generate_sql(self, question: str, schema: str) -> str:
        return self.sql

    def answer_from_rows(self, question: str, rows) -> str:
        return f"Rows: {list(rows)}"


class FakeGuardJudge:
    """Stand-in for the OpenEvals guardrail judge.

    ``input_result`` / ``output_result`` may be a verdict payload (dict, string,
    ``None``) or an exception instance, which is raised to exercise fail-closed.
    """

    def __init__(self, input_result=None, output_result=None):
        allow = {"verdict": "allow", "category": "none", "reason": "Looks fine."}
        self.input_result = allow if input_result is None else input_result
        self.output_result = allow if output_result is None else output_result
        self.input_calls: list[tuple] = []
        self.output_calls: list[tuple] = []

    def _respond(self, result):
        if isinstance(result, BaseException):
            raise result
        return result

    def judge_input(self, question, role, collections):
        self.input_calls.append((question, role, collections))
        return self._respond(self.input_result)

    def judge_output(self, question, answer, context, role, collections):
        self.output_calls.append((question, answer, context, role, collections))
        return self._respond(self.output_result)


@pytest.fixture
def fake_guard_judge() -> FakeGuardJudge:
    return FakeGuardJudge()


@pytest.fixture
def event_logger(tmp_path):
    from mediassist.observability import EventLogger

    return EventLogger(tmp_path / "logs")


@pytest.fixture
def guardrails(fake_guard_judge, event_logger):
    from mediassist.config import get_settings
    from mediassist.guardrails import GuardrailService

    return GuardrailService(get_settings(), event_logger, judge=fake_guard_judge, llm_enabled=True)


@pytest.fixture
def sample_chunks() -> list[dict]:
    return [dict(chunk) for chunk in SAMPLE_CHUNKS]


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture(scope="session")
def main_module():
    """Import the API with ingestion stubbed so no documents are parsed."""

    from mediassist import ingestion

    original = ingestion.ingest_documents
    ingestion.ingest_documents = lambda _data_dir, _cache_file=None: [dict(chunk) for chunk in SAMPLE_CHUNKS]
    try:
        from mediassist import main
    finally:
        ingestion.ingest_documents = original
    return main


@pytest.fixture
def client(main_module, fake_llm, sample_chunks, guardrails, event_logger, monkeypatch):
    """A TestClient wired to the fallback retriever, a fake LLM, and a fake guardrail judge."""

    from fastapi.testclient import TestClient

    from mediassist import rbac
    from mediassist.retrieval import HybridRetriever

    monkeypatch.setattr(main_module, "retriever", HybridRetriever(sample_chunks))
    monkeypatch.setattr(main_module, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(main_module, "get_guardrails", lambda: guardrails)
    monkeypatch.setattr(main_module, "get_event_logger", lambda: event_logger)
    rbac.ACTIVE_SESSIONS.clear()
    with TestClient(main_module.app) as test_client:
        yield test_client
    rbac.ACTIVE_SESSIONS.clear()

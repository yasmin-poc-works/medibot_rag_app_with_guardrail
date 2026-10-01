"""Tests for ingestion fallbacks, settings resolution, and the Groq gateway."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mediassist import ingestion
from mediassist.config import ROOT_DIR, Settings, get_settings
from mediassist.llm import GroqLLM


# --- ingestion -------------------------------------------------------------


def test_metadata_uses_last_heading_and_parent_collection(tmp_path):
    path = tmp_path / "billing" / "guide.md"
    meta = SimpleNamespace(headings=["Guide", "Submitting"])

    chunk = ingestion._metadata(path, 2, "body", meta)

    assert chunk == {
        "text": "body",
        "source_document": "guide.md",
        "section_title": "Submitting",
        "collection": "billing",
        "chunk_type": "text",
        "chunk_index": 2,
    }


def test_metadata_defaults_section_title():
    assert ingestion._metadata(Path("general/a.pdf"), 0, "x")["section_title"] == "Document"


def test_fallback_chunks_reads_markdown_and_skips_other_files(tmp_path):
    (tmp_path / "billing").mkdir()
    (tmp_path / "billing" / "guide.md").write_text("Submit claims within 30 days.", encoding="utf-8")
    (tmp_path / "billing" / "notes.txt").write_text("ignored", encoding="utf-8")

    chunks = ingestion._fallback_chunks(tmp_path)

    assert [(c["source_document"], c["collection"], c["text"]) for c in chunks] == [
        ("guide.md", "billing", "Submit claims within 30 days.")
    ]


def test_fallback_chunks_extracts_pdf_pages():
    chunks = ingestion._fallback_chunks(ROOT_DIR / "mediassist_data" / "general")

    assert chunks
    assert {chunk["collection"] for chunk in chunks} == {"general"}
    assert all(chunk["text"].strip() for chunk in chunks)


def test_ingest_documents_falls_back_when_docling_is_unavailable(tmp_path, monkeypatch):
    (tmp_path / "general").mkdir()
    (tmp_path / "general" / "faq.md").write_text("Parking is free.", encoding="utf-8")
    # A ``None`` entry in ``sys.modules`` makes the import raise ImportError.
    monkeypatch.setitem(sys.modules, "docling.chunking", None)

    chunks = ingestion.ingest_documents(tmp_path)

    assert [chunk["source_document"] for chunk in chunks] == ["faq.md"]


def test_ingest_documents_reuses_cached_chunks_without_parsing(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "general").mkdir(parents=True)
    source = data_dir / "general" / "faq.md"
    source.write_text("Parking is free.", encoding="utf-8")
    cache_file = tmp_path / "cache.json"
    cached_chunk = ingestion._metadata(source, 0, "cached text")
    ingestion._save_cache(
        cache_file,
        {"general/faq.md": {"fingerprint": ingestion._fingerprint(source), "chunks": [cached_chunk]}},
    )

    def fail_build():
        raise AssertionError("Docling should not run for cached files")

    monkeypatch.setattr(ingestion, "_build_converter", fail_build)

    assert ingestion.ingest_documents(data_dir, cache_file) == [cached_chunk]


def test_attach_parent_sections_prefixes_subsections_with_enumerated_parent():
    path = Path("clinical/protocols.pdf")
    raw = [
        ingestion._metadata(path, 0, "Standard Treatment Protocols", SimpleNamespace(headings=["Standard Treatment Protocols"])),
        ingestion._metadata(path, 1, "B. Hypertension - Stage 2\nICD-10: I10", SimpleNamespace(headings=["B. Hypertension - Stage 2"])),
        ingestion._metadata(path, 2, "Diagnostic criteria\nSystolic BP >=140", SimpleNamespace(headings=["Diagnostic criteria"])),
    ]

    chunks = ingestion._attach_parent_sections(raw)

    assert [c["section_title"] for c in chunks] == [
        "Standard Treatment Protocols",
        "B. Hypertension - Stage 2",
        "B. Hypertension - Stage 2 > Diagnostic criteria",
    ]
    assert chunks[2]["text"] == "B. Hypertension - Stage 2\nDiagnostic criteria\nSystolic BP >=140"
    assert raw[2]["section_title"] == "Diagnostic criteria"  # the cached raw chunk is untouched


def test_load_cache_ignores_corrupt_file(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text("not json", encoding="utf-8")

    assert ingestion._load_cache(cache_file) == {}


# --- config ----------------------------------------------------------------


def _settings(**overrides) -> Settings:
    values = {
        "GROQ_API_KEY": "",
        "GROQ_MODEL": "",
        "QDRANT_URL": "http://localhost:6333",
        "QDRANT_COLLECTION": "c",
        "DATABASE_PATH": "mediassist_db/db/mediassist.db",
        "FRONTEND_ORIGIN": "http://localhost:3000",
    }
    values.update(overrides)
    return Settings(**values)


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_database_file_resolves_relative_to_backend():
    settings = _settings()

    assert settings.database_file == ROOT_DIR / "mediassist_db" / "db" / "mediassist.db"
    assert settings.database_file.exists()


def test_database_file_keeps_absolute_path(tmp_path):
    absolute = tmp_path / "x.db"

    assert _settings(DATABASE_PATH=str(absolute)).database_file == absolute


def test_local_qdrant_path_prefers_explicit_setting(tmp_path):
    assert _settings(QDRANT_PATH=str(tmp_path)).local_qdrant_path == str(tmp_path)


def test_local_qdrant_path_defaults_to_temp_dir():
    assert _settings(QDRANT_PATH="").local_qdrant_path.endswith("mediassist_qdrant")


# --- llm -------------------------------------------------------------------


class _FakeCompletions:
    def __init__(self, content=None, error: Exception | None = None):
        self.content = content
        self.error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _llm_with(completions: _FakeCompletions) -> GroqLLM:
    llm = GroqLLM(_settings(GROQ_MODEL="test-model"))
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return llm


def test_llm_without_key_returns_offline_message():
    llm = GroqLLM(_settings())

    assert llm.client is None
    assert llm.complete("s", "u").startswith("Groq is not configured.")


def test_llm_sends_system_and_user_messages():
    completions = _FakeCompletions(content="hello")

    assert _llm_with(completions).complete("sys", "usr") == "hello"
    assert completions.kwargs["model"] == "test-model"
    assert completions.kwargs["temperature"] == 0
    assert completions.kwargs["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]


def test_llm_handles_empty_content():
    assert _llm_with(_FakeCompletions(content=None)).complete("s", "u") == "No answer was generated."


def test_llm_handles_client_errors():
    answer = _llm_with(_FakeCompletions(error=RuntimeError("network down"))).complete("s", "u")

    assert answer.startswith("The language model is unavailable right now.")


@pytest.mark.parametrize(
    "method,args,expected_in_prompt",
    [
        ("generate_sql", ("How many claims?", "claims(id)"), "Schema:\nclaims(id)"),
        ("answer_from_rows", ("How many claims?", [{"n": 1}]), "Rows: [{'n': 1}]"),
    ],
)
def test_llm_helper_prompts(method, args, expected_in_prompt):
    completions = _FakeCompletions(content="ok")

    assert getattr(_llm_with(completions), method)(*args) == "ok"
    assert expected_in_prompt in completions.kwargs["messages"][1]["content"]

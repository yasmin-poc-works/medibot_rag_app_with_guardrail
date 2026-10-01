"""Tests for the deterministic lexical retrieval path and RBAC filtering."""

from mediassist.retrieval import HybridRetriever, RetrievedChunk, _expanded_terms, _terms
from mediassist.schemas import SourceCitation


def test_terms_drop_stop_words_and_punctuation():
    assert _terms("What is the CVC procedure?") == {"cvc", "procedure"}


def test_expanded_terms_add_domain_synonyms():
    expanded = _expanded_terms({"cvc"})

    assert {"central", "venous", "catheter"} <= expanded


def test_retriever_without_settings_uses_fallback(sample_chunks):
    retriever = HybridRetriever(sample_chunks)

    results = retriever.retrieve("STEMI protocol", "doctor")

    assert retriever._vectorstore is None
    assert results[0].source_document == "treatment_protocols.pdf"


def test_retrieval_filters_by_role(sample_chunks):
    retriever = HybridRetriever(sample_chunks)

    # The only matching chunk is in "billing", which technicians cannot read.
    assert retriever.retrieve("claim submission diagnosis code", "technician") == []
    assert retriever.retrieve("claim submission diagnosis code", "billing_executive")[0].collection == "billing"


def test_abbreviation_expansion_finds_full_wording(sample_chunks):
    chunk = dict(sample_chunks[0], text="Central venous catheter dressing steps.", section_title="Line Care")
    retriever = HybridRetriever([chunk])

    results = retriever.retrieve("CVC procedure", "nurse")

    assert [result.source_document for result in results] == ["icu_nursing_procedures.pdf"]


def test_retrieval_respects_limit_and_orders_by_score(sample_chunks):
    retriever = HybridRetriever(sample_chunks)

    results = retriever.retrieve("steps protocol maintenance leave claims catheter", "admin", limit=2)

    assert len(results) == 2
    assert results[0].score >= results[1].score


def test_retrieval_with_no_matches_returns_empty(sample_chunks):
    assert HybridRetriever(sample_chunks).retrieve("zzzz qqqq", "admin") == []


def test_retrieval_with_no_chunks_returns_empty():
    assert HybridRetriever().retrieve("anything", "admin") == []


def test_citation_contains_only_public_fields():
    chunk = RetrievedChunk(text="t", source_document="a.pdf", section_title="S", collection="general", score=1.0)

    assert chunk.citation() == SourceCitation(source_document="a.pdf", section_title="S", collection="general")

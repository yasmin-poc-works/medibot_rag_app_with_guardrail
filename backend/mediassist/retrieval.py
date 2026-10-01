"""Notebook-style hybrid retrieval with safe local fallbacks.

The primary path mirrors ``advanced_rag.ipynb``: dense HuggingFace vectors
plus FastEmbed BM25 sparse vectors in a local Qdrant collection, followed by
cross-encoder reranking. Imports and model initialization are lazy so API
tests and offline development still work without downloading models.
"""

import re
from dataclasses import dataclass

from langsmith import traceable

from .observability import stage_timer
from .rbac import allowed_collections
from .schemas import Role, SourceCitation


STOP_WORDS = {
    "a", "an", "and", "are", "can", "do", "for", "how", "i", "in",
    "is", "me", "of", "on", "please", "the", "to", "what", "when",
    "where", "which", "with", "you", "your",
}

# Expand common clinical abbreviations and wording differences before the
# lexical pass. This keeps the deterministic fallback useful for questions
# that do not repeat a document heading verbatim.
QUERY_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "cvc": ("cvc", "central", "venous", "catheter"),
    "central": ("central", "venous", "catheter"),
    "venous": ("central", "venous", "catheter"),
    "catheter": ("central", "venous", "catheter"),
    "procedure": ("procedure", "steps", "protocol", "sop"),
    "procedures": ("procedure", "procedures", "steps", "protocol", "sop"),
    "precautions": ("precautions", "prevention", "infection", "hygiene"),
}


def _terms(text: str) -> set[str]:
    """Tokenize useful content while ignoring conversational filler words."""

    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in STOP_WORDS
    }


def _expanded_terms(question_terms: set[str]) -> set[str]:
    """Add small, domain-focused expansions without requiring an embedding model."""

    expanded = set(question_terms)
    for term in question_terms:
        expanded.update(QUERY_EXPANSIONS.get(term, ()))
    return expanded


@dataclass
class RetrievedChunk:
    """A chunk plus its reranker score."""

    text: str
    source_document: str
    section_title: str
    collection: str
    score: float

    def citation(self) -> SourceCitation:
        """Return the public citation representation."""

        return SourceCitation(source_document=self.source_document, section_title=self.section_title, collection=self.collection)


class HybridRetriever:
    """Retrieve role-filtered documents with hybrid search and reranking."""

    def __init__(self, chunks: list[dict] | None = None, settings: object | None = None):
        """Initialize the local Qdrant index lazily and retain a deterministic fallback."""

        self.chunks = chunks or []
        self.settings = settings
        self._vectorstore = None
        self._reranker = None
        self._hybrid_ready = False

    def _build_hybrid_index(self) -> None:
        """Build the notebook's dense+sparse local Qdrant index once."""

        if self._hybrid_ready:
            return
        self._hybrid_ready = True
        # Direct unit-test instances intentionally use only the deterministic
        # fallback. The API supplies Settings to enable the notebook pipeline.
        if self.settings is None:
            return
        try:
            from langchain_core.documents import Document
            from langchain_huggingface import HuggingFaceEmbeddings
            from langchain_qdrant import QdrantVectorStore, RetrievalMode
            from langchain_qdrant import FastEmbedSparse

            embed_model = getattr(self.settings, "embed_model", "sentence-transformers/all-MiniLM-L6-v2")
            sparse_model = getattr(self.settings, "sparse_model", "Qdrant/bm25")
            collection = getattr(self.settings, "qdrant_collection", "mediassist_documents")
            qdrant_path = getattr(self.settings, "local_qdrant_path", None)
            documents = [
                Document(
                    page_content=chunk["text"],
                    metadata={
                        "source_document": chunk["source_document"],
                        "section_title": chunk["section_title"],
                        "collection": chunk["collection"],
                        "chunk_index": chunk.get("chunk_index", 0),
                    },
                )
                for chunk in self.chunks
            ]
            dense = HuggingFaceEmbeddings(
                model_name=embed_model,
                model_kwargs={"device": "cpu"},
                encode_kwargs={"normalize_embeddings": True},
            )
            sparse = FastEmbedSparse(model_name=sparse_model, batch_size=32)
            vectorstore_args = {
                "documents": documents,
                "embedding": dense,
                "sparse_embedding": sparse,
                "path": qdrant_path,
                "collection_name": collection,
                "retrieval_mode": RetrievalMode.HYBRID,
            }
            try:
                self._vectorstore = QdrantVectorStore.from_documents(
                    **vectorstore_args, force_recreate=True
                )
            except TypeError:
                # Older langchain-qdrant releases do not expose this keyword.
                self._vectorstore = QdrantVectorStore.from_documents(**vectorstore_args)
        except Exception:
            # Missing optional packages, model downloads, or a locked local
            # Qdrant directory should not prevent the API from starting.
            self._vectorstore = None

    @traceable(name="MediBot: Cross-Encoder Rerank", run_type="chain")
    def _rerank(self, question: str, documents: list[object], limit: int) -> list[object]:
        """Apply the notebook's cross-encoder reranker when available."""

        with stage_timer("rerank"):
            return self._rerank_documents(question, documents, limit)

    def _rerank_documents(self, question: str, documents: list[object], limit: int) -> list[object]:
        try:
            from langchain_community.cross_encoders import HuggingFaceCrossEncoder
            from langchain_classic.retrievers.document_compressors import CrossEncoderReranker

            if self._reranker is None:
                model_name = getattr(self.settings, "reranker_model", "cross-encoder/ms-marco-MiniLM-L-6-v2")
                model = HuggingFaceCrossEncoder(model_name=model_name)
                self._reranker = CrossEncoderReranker(model=model, top_n=limit)
            return self._reranker.compress_documents(documents, question)
        except Exception:
            return documents[:limit]

    def _fallback_retrieve(self, question: str, role: Role, limit: int) -> list[RetrievedChunk]:
        """Use the original deterministic lexical path when hybrid search is unavailable."""

        permitted = set(allowed_collections(role))
        query_terms = _terms(question)
        expanded_query_terms = _expanded_terms(query_terms)
        candidates = []
        for chunk in self.chunks:
            if chunk["collection"] not in permitted:
                continue
            searchable_text = f'{chunk["source_document"]} {chunk["section_title"]} {chunk["text"]}'
            terms = _terms(searchable_text)
            exact_matches = query_terms & terms
            expanded_matches = (expanded_query_terms - query_terms) & terms
            # Exact user wording matters most; expansions bridge abbreviations
            # such as CVC -> central venous catheter and procedure -> steps.
            score = (2 * len(exact_matches) + len(expanded_matches)) / max(2 * len(query_terms), 1)
            if chunk["section_title"].lower() != "document" and exact_matches:
                score += 0.15
            if score > 0:
                candidates.append((score, chunk))
        candidates.sort(key=lambda item: item[0], reverse=True)
        # Ingestion keeps additional indexing metadata (for example
        # ``chunk_type`` and ``chunk_index``). Only pass the public retrieval
        # fields into the response model so Docling and fallback chunks share
        # the same runtime contract.
        return [
            RetrievedChunk(
                text=chunk["text"],
                source_document=chunk["source_document"],
                section_title=chunk["section_title"],
                collection=chunk["collection"],
                score=score,
            )
            for score, chunk in candidates[:limit]
        ]

    @traceable(name="MediBot: Hybrid Retrieval", run_type="retriever")
    def retrieve(self, question: str, role: Role, limit: int = 3) -> list[RetrievedChunk]:
        """Run hybrid search, role filtering, reranking, and citation mapping."""

        self._build_hybrid_index()
        if self._vectorstore is None:
            with stage_timer("retrieval"):
                return self._fallback_retrieve(question, role, limit)
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            permitted = allowed_collections(role)
            role_filter = Filter(
                should=[FieldCondition(key="metadata.collection", match=MatchValue(value=name)) for name in permitted]
            )
            retriever = self._vectorstore.as_retriever(
                search_kwargs={"k": 10, "filter": role_filter}
            )
            with stage_timer("retrieval"):
                candidates = retriever.invoke(question)
            reranked = self._rerank(question, candidates, limit)
            return [
                RetrievedChunk(
                    text=doc.page_content,
                    source_document=doc.metadata.get("source_document", "unknown"),
                    section_title=doc.metadata.get("section_title", "Document"),
                    collection=doc.metadata.get("collection", "unknown"),
                    score=0.0,
                )
                for doc in reranked[:limit]
            ]
        except Exception:
            return self._fallback_retrieve(question, role, limit)

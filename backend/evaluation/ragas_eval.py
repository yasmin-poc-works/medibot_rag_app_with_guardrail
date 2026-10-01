"""RAGAS retrieval and answer-quality metrics.

Computes faithfulness, answer relevancy, context precision and context recall for
every answerable question that reached the model, using the separate judge model on
Groq (temperature 0) and the same local MiniLM embeddings as the retriever.
"""

import math
import warnings
from typing import Any

from . import _compat  # noqa: F401  (must precede any ragas import)

METRIC_NAMES = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


def ragas_ready(record: dict[str, Any]) -> bool:
    """RAGAS needs an answer, retrieved contexts, and a reference."""

    return (
        record.get("expected_behavior") == "answer"
        and record.get("guardrail") == "allowed"
        and bool(record.get("contexts"))
        and bool((record.get("answer") or "").strip())
    )


def _clean(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else round(number, 4)


def build_metrics(settings, embeddings=None):
    """Create the four RAGAS metrics bound to the judge LLM and local embeddings."""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness

    from mediassist.llm import build_judge_chat_model

    if embeddings is None:
        from langchain_huggingface import HuggingFaceEmbeddings

        embeddings = HuggingFaceEmbeddings(
            model_name=settings.embed_model,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
    llm = LangchainLLMWrapper(build_judge_chat_model(settings))
    ragas_embeddings = LangchainEmbeddingsWrapper(embeddings)
    return [
        Faithfulness(llm=llm),
        # strictness=1: one generated question per answer (Groq does not support n>1).
        AnswerRelevancy(llm=llm, embeddings=ragas_embeddings, strictness=1),
        ContextPrecision(llm=llm),
        ContextRecall(llm=llm),
    ]


def run_ragas(records: list[dict[str, Any]], settings, max_workers: int = 2) -> dict[str, Any]:
    """Score every RAGAS-ready record; return per-question and aggregate scores."""

    items = [record for record in records if ragas_ready(record)]
    if not items:
        return {"per_item": [], "aggregate": {name: None for name in METRIC_NAMES}, "evaluated": 0, "skipped": len(records)}

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from ragas import EvaluationDataset, RunConfig, evaluate

    dataset = EvaluationDataset.from_list(
        [
            {
                "user_input": item["question"],
                "retrieved_contexts": item["contexts"],
                "response": item["answer"],
                "reference": item["ground_truth"],
            }
            for item in items
        ]
    )
    result = evaluate(
        dataset=dataset,
        metrics=build_metrics(settings),
        run_config=RunConfig(max_workers=max_workers, timeout=180, max_retries=3, max_wait=30, seed=42),
        raise_exceptions=False,
        show_progress=True,
    )
    per_item = []
    for item, scores in zip(items, result.scores):
        per_item.append({"id": item["id"], **{name: _clean(scores.get(name)) for name in METRIC_NAMES}})
    aggregate = {}
    for name in METRIC_NAMES:
        values = [row[name] for row in per_item if row[name] is not None]
        aggregate[name] = round(sum(values) / len(values), 4) if values else None
    return {
        "per_item": per_item,
        "aggregate": aggregate,
        "evaluated": len(items),
        "skipped": len(records) - len(items),
        "failed_scores": {name: sum(1 for row in per_item if row[name] is None) for name in METRIC_NAMES},
    }

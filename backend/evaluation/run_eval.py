"""Repeatable evaluation pipeline for MediBot.

Order of stages (cheapest first, so a broken system fails fast):

1. Run every labeled question through the real guarded pipeline (in process, so the
   retrieved contexts are available) - each run is a LangSmith trace + log record.
2. Heuristic checks (no LLM). Empty answers or restricted sources stop the run here.
3. RAGAS: faithfulness, answer relevancy, context precision, context recall.
4. LLM-as-a-judge over the labeled set and the known-bad calibration set.
5. Adversarial guardrail suite, including fail-closed probes.
6. Consolidated Markdown report with an overall PASS/FAIL verdict.

Usage (from ``backend/``)::

    uv run --env-file .env python -m evaluation.run_eval            # full run
    uv run --env-file .env python -m evaluation.run_eval --repeat 2 # repeatability check
    uv run --env-file .env python -m evaluation.run_eval --ids q01,q22 --skip-ragas
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import _compat  # noqa: F401  (patch before anything imports ragas)
from .dataset import load_calibration_set, load_eval_set
from .guardrail_suite import run_suite
from .heuristics import fail_fast_reasons, run_calibration, run_heuristics
from .judge import LLMJudge, summarise
from .ragas_eval import METRIC_NAMES, run_ragas
from .report import compute_verdict, render_markdown

EVAL_DIR = Path(__file__).resolve().parent
RUNS_DIR = EVAL_DIR / "runs"
REPORTS_DIR = EVAL_DIR / "reports"


def collect_responses(pipeline, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ask the real system every labeled question and keep what it saw and answered."""

    records = []
    for index, item in enumerate(items, start=1):
        result = pipeline.run(item["question"], item["role"], channel="evaluation")
        records.append(
            {
                **item,
                "answer": result.answer,
                "sources": [source.model_dump() for source in result.sources],
                "contexts": result.contexts,
                "retrieval_type": result.retrieval_type,
                "guardrail": result.guardrail,
                "blocked_stage": result.blocked_stage,
                "block_category": result.block_category,
                "latency_ms": result.latency_ms.get("total", 0.0),
                "stage_latency_ms": result.latency_ms,
                "tokens": result.tokens,
                "request_id": result.request_id,
                "trace_id": result.trace_id,
            }
        )
        print(f"  [{index:>2}/{len(items)}] {item['id']} {result.guardrail:<8} {result.retrieval_type:<10} {result.latency_ms.get('total', 0):>8.0f} ms", flush=True)
    return records


def score_run(records, settings, calibration, args) -> dict[str, Any]:
    """Heuristics first; then RAGAS and the judge unless the system failed fast."""

    run: dict[str, Any] = {"records": records}
    print("-> heuristic checks", flush=True)
    run["heuristics"] = run_heuristics(records, settings.latency_threshold_ms)
    run["calibration"] = {"heuristics": run_calibration(calibration, settings.latency_threshold_ms)}
    run["fail_fast_reasons"] = [] if args.no_fail_fast else fail_fast_reasons(run["heuristics"])
    if run["fail_fast_reasons"]:
        print(f"!! fail-fast: {run['fail_fast_reasons']} - skipping RAGAS and judge", flush=True)
        return run

    if not args.skip_ragas:
        print("-> RAGAS", flush=True)
        run["ragas"] = run_ragas(records, settings, max_workers=args.workers)
    if not args.skip_judge:
        print("-> LLM-as-a-judge", flush=True)
        judge = LLMJudge.from_settings(settings)
        per_item = judge.judge_all(records)
        run["judge"] = {"per_item": per_item, "summary": summarise(per_item)}
        calibration_records = [{**case, **case["response"]} for case in calibration]
        run["calibration"]["judge"] = [
            {**result, "description": case["description"], "judge_should_fail": case["judge_should_fail"]}
            for case, result in zip(calibration, judge.judge_all(calibration_records))
        ]
    return run


def aggregates(run: dict[str, Any]) -> dict[str, float]:
    values: dict[str, float] = {}
    for metric in METRIC_NAMES:
        value = (run.get("ragas") or {}).get("aggregate", {}).get(metric)
        if value is not None:
            values[metric] = round(value, 4)
    if run.get("judge"):
        # Normalise the 1-5 judge mean to 0-1 so one delta threshold fits every metric.
        values["judge_mean_overall_norm"] = round((run["judge"]["summary"]["mean_overall"] - 1) / 4, 4)
    return values


def repeatability(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(runs) < 2:
        return None
    per_run = [aggregates(run) for run in runs]
    metrics = sorted(set.intersection(*(set(values) for values in per_run))) if per_run else []
    deltas = {m: round(max(v[m] for v in per_run) - min(v[m] for v in per_run), 4) for m in metrics}
    return {"runs": per_run, "deltas": deltas, "max_delta": max(deltas.values()) if deltas else 0.0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the MediBot evaluation pipeline.")
    parser.add_argument("--repeat", type=int, default=1, help="Run the whole evaluation N times and report score drift.")
    parser.add_argument("--ids", help="Comma-separated subset of question ids (e.g. q01,q22).")
    parser.add_argument("--skip-ragas", action="store_true")
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--skip-suite", action="store_true", help="Skip the adversarial guardrail suite.")
    parser.add_argument("--no-fail-fast", action="store_true", help="Run LLM evals even if fail-fast heuristics fail.")
    parser.add_argument("--workers", type=int, default=2, help="Parallel RAGAS workers (keep low for Groq rate limits).")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    items = load_eval_set()
    if args.ids:
        wanted = {value.strip() for value in args.ids.split(",")}
        items = [item for item in items if item["id"] in wanted]
    calibration = load_calibration_set()

    print("Loading MediBot (document ingestion + hybrid index)...", flush=True)
    from mediassist import main as app_main

    settings = app_main.settings
    pipeline = app_main.get_pipeline()
    # Build the index, load the reranker, and create the guardrail judge before any request is timed,
    # so one-off cold-start costs do not count against the latency heuristic.
    app_main.retriever.retrieve("warm-up query for the hybrid index", "admin")
    if pipeline.guardrails.llm_enabled:
        pipeline.guardrails.check_input("What is the leave policy?", "admin", "warm-up")

    runs = []
    for attempt in range(1, args.repeat + 1):
        print(f"\n=== Run {attempt}/{args.repeat}: {len(items)} labeled questions ===", flush=True)
        records = collect_responses(pipeline, items)
        runs.append(score_run(records, settings, calibration, args))

    results: dict[str, Any] = dict(runs[-1])
    if not args.skip_suite:
        print("-> adversarial guardrail suite", flush=True)
        results["guardrail_suite"] = run_suite(settings)
    results["repeatability"] = repeatability(runs)
    results["live_metrics"] = app_main.get_event_logger().metrics()
    results["meta"] = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "chat_model": settings.groq_model,
        "judge_model": settings.judge_model,
        "questions": len(items),
        "repeats": args.repeat,
        "duration_s": time.perf_counter() - started,
    }
    results["verdict"] = compute_verdict(results)

    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS_DIR / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.json").write_text(json.dumps({**results, "all_runs": runs}, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(results)
    (REPORTS_DIR / f"eval_report_{stamp}.md").write_text(markdown, encoding="utf-8")
    (REPORTS_DIR / "latest.md").write_text(markdown, encoding="utf-8")

    print(f"\nOverall verdict: {results['verdict']['overall']}")
    if results["verdict"]["failed_checks"]:
        print("Failed checks: " + ", ".join(results["verdict"]["failed_checks"]))
    print(f"Report:  {REPORTS_DIR / f'eval_report_{stamp}.md'}")
    print(f"Results: {run_dir / 'results.json'}")
    return 0 if results["verdict"]["overall"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

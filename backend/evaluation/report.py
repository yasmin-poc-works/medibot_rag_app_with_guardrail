"""Consolidate every evaluation signal into one pass/fail report (Markdown)."""

import json
from collections import Counter
from typing import Any

from mediassist.guardrails import SAFE_REFUSAL

from . import thresholds as t


def _check(name: str, group: str, threshold: str, actual: Any, passed: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "group": group, "threshold": threshold, "actual": actual, "passed": bool(passed), "detail": detail}


def compute_verdict(results: dict[str, Any]) -> dict[str, Any]:
    """Apply the thresholds to every signal and return per-check and overall verdicts."""

    checks: list[dict[str, Any]] = []

    if results.get("fail_fast_reasons"):
        checks.append(_check("fail_fast", "heuristics", "no broken-system failures", "; ".join(results["fail_fast_reasons"]), False,
                             "LLM evaluations skipped because the system failed fast checks."))

    heuristics = results["heuristics"]["summary"]
    for name, stats in heuristics.items():
        if not stats["applicable"]:
            continue
        critical = name in t.CRITICAL_HEURISTICS
        limit = 1.0 if critical else t.HEURISTIC_PASS_RATE_THRESHOLD
        checks.append(_check(
            f"heuristic:{name}", "heuristics", f">= {limit:.0%}{' (critical)' if critical else ''}",
            f"{stats['passed']}/{stats['applicable']} ({stats['pass_rate']:.0%})", stats["pass_rate"] >= limit,
        ))

    ragas = results.get("ragas")
    if ragas is not None:
        for metric, limit in t.RAGAS_THRESHOLDS.items():
            value = ragas["aggregate"].get(metric)
            checks.append(_check(f"ragas:{metric}", "ragas", f">= {limit:.2f}", "not computed" if value is None else round(value, 3),
                                 value is not None and value >= limit))

    judge = results.get("judge")
    if judge is not None:
        summary = judge["summary"]
        checks.append(_check("judge:mean_overall", "judge", f">= {t.JUDGE_MEAN_THRESHOLD}/5", summary["mean_overall"],
                             summary["mean_overall"] >= t.JUDGE_MEAN_THRESHOLD))
        checks.append(_check("judge:pass_rate", "judge", f">= {t.JUDGE_PASS_RATE_THRESHOLD:.0%}", f"{summary['pass_rate']:.0%}",
                             summary["pass_rate"] >= t.JUDGE_PASS_RATE_THRESHOLD))

    calibration = results.get("calibration", {})
    if calibration.get("heuristics"):
        caught = sum(1 for row in calibration["heuristics"] if row["caught"])
        total = len(calibration["heuristics"])
        checks.append(_check("calibration:heuristics_catch_bad_responses", "calibration", "100% of known-bad responses caught",
                             f"{caught}/{total}", caught == total))
    if calibration.get("judge"):
        expected_fail = [row for row in calibration["judge"] if row["judge_should_fail"]]
        caught = sum(1 for row in expected_fail if not row["passed"])
        checks.append(_check("calibration:judge_catches_wrong_answers", "calibration", "judge fails every known-wrong answer",
                             f"{caught}/{len(expected_fail)}", caught == len(expected_fail)))

    suite = results.get("guardrail_suite")
    if suite is not None:
        summary = suite["summary"]
        checks.append(_check("guardrail:attack_block_rate", "guardrails", f">= {t.GUARDRAIL_ATTACK_BLOCK_RATE:.0%}",
                             f"{summary['attacks_blocked']}/{summary['attacks']}", summary["attack_block_rate"] >= t.GUARDRAIL_ATTACK_BLOCK_RATE))
        checks.append(_check("guardrail:false_block_rate", "guardrails", f"<= {t.GUARDRAIL_MAX_FALSE_BLOCK_RATE:.0%}",
                             f"{summary['benign_blocked']}/{summary['benign']}", summary["false_block_rate"] <= t.GUARDRAIL_MAX_FALSE_BLOCK_RATE))
        checks.append(_check("guardrail:fail_closed", "guardrails", "every malformed verdict blocked",
                             f"{summary['fail_closed_correct']}/{summary['fail_closed_probes']}",
                             summary["fail_closed_correct"] == summary["fail_closed_probes"]))

    repeat = results.get("repeatability")
    if repeat:
        checks.append(_check("repeatability:max_delta", "repeatability", f"<= {t.REPEATABILITY_MAX_DELTA}", repeat["max_delta"],
                             repeat["max_delta"] <= t.REPEATABILITY_MAX_DELTA))

    failed = [check for check in checks if not check["passed"]]
    return {"overall": "PASS" if not failed else "FAIL", "checks": checks, "failed_checks": [check["name"] for check in failed]}


def guardrail_counts(records: list[dict[str, Any]]) -> dict[str, Any]:
    blocked = [r for r in records if r.get("guardrail") == "blocked"]
    return {
        "requests": len(records),
        "allowed": len(records) - len(blocked),
        "blocked": len(blocked),
        "blocked_by_stage": dict(Counter(r.get("blocked_stage") or "input" for r in blocked)),
        "blocked_by_category": dict(Counter(r.get("block_category") or "unknown" for r in blocked)),
    }


# ----------------------------------------------------------------------- markdown


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        return text.replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(cell(value) for value in row) + " |" for row in rows]
    return "\n".join(lines)


def _short(text: str, limit: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_markdown(results: dict[str, Any]) -> str:
    verdict = results["verdict"]
    meta = results["meta"]
    records = results["records"]
    out: list[str] = []
    badge = "✅ PASS" if verdict["overall"] == "PASS" else "❌ FAIL"
    out.append("# MediBot Evaluation & Guardrail Report\n")
    out.append(f"**Overall verdict: {badge}**\n")
    out.append(_table(["Item", "Value"], [
        ["Run started", meta["started_at"]],
        ["Chatbot model (GROQ_MODEL)", meta["chat_model"]],
        ["Judge / guardrail / RAGAS model (JUDGE_MODEL)", meta["judge_model"]],
        ["Labeled questions", meta["questions"]],
        ["Repeats", meta["repeats"]],
        ["Duration", f"{meta['duration_s']:.0f} s"],
    ]))
    out.append("")
    if verdict["failed_checks"]:
        out.append("**Failed checks:** " + ", ".join(f"`{name}`" for name in verdict["failed_checks"]) + "\n")
    else:
        out.append("All checks met their thresholds.\n")

    out.append("## 1. Scorecard\n")
    out.append(_table(["Check", "Group", "Threshold", "Actual", "Result"], [
        [c["name"], c["group"], c["threshold"], c["actual"], "PASS" if c["passed"] else "**FAIL**"] for c in verdict["checks"]
    ]))
    out.append("")

    # Guardrails
    out.append("## 2. Guardrails\n")
    counts = guardrail_counts(records)
    out.append("### 2.1 Block / allow counts\n")
    rows = [["Evaluation run (labeled set)", counts["requests"], counts["allowed"], counts["blocked"],
             json.dumps(counts["blocked_by_category"])]]
    suite = results.get("guardrail_suite")
    if suite:
        cases = suite["input_cases"] + suite["output_cases"]
        blocked = sum(1 for c in cases if c["actual"] == "block")
        rows.append(["Adversarial suite (input + output cases)", len(cases), len(cases) - blocked, blocked,
                     json.dumps(dict(Counter(c["decision"]["category"] for c in cases if c["actual"] == "block")))])
    live = results.get("live_metrics")
    if live and live.get("requests"):
        rows.append(["All logged traffic (logs/requests.jsonl)", live["requests"], live["requests"] - live["blocked_requests"],
                     live["blocked_requests"], json.dumps(live["block_categories"])])
    out.append(_table(["Source", "Requests", "Allowed", "Blocked", "Block categories"], rows))
    out.append("")
    if suite:
        s = suite["summary"]
        out.append(f"Adversarial suite: **{s['attacks_blocked']}/{s['attacks']}** attacks blocked, "
                   f"**{s['benign_blocked']}/{s['benign']}** benign prompts wrongly blocked, "
                   f"**{s['fail_closed_correct']}/{s['fail_closed_probes']}** fail-closed probes blocked.\n")
        out.append("### 2.2 Adversarial suite results\n")
        out.append(_table(["ID", "Type", "Role", "Prompt / answer", "Expected", "Actual", "Deciding check", "Category"], [
            [c["id"], c["attack_type"], c["role"], _short(c.get("question") if c["id"].startswith("g") else c.get("answer"), 90),
             c["expected"], c["actual"] + ("" if c["correct"] else " ⚠"), c["decision"]["check"], c["decision"]["category"]]
            for c in suite["input_cases"] + suite["output_cases"]
        ]))
        out.append("\n### 2.3 Fail-closed probes (judge returns malformed output)\n")
        out.append(_table(["ID", "Probe", "Raw judge output", "Result", "Logged reason"], [
            [f["id"], f["probe"], _short(f["raw_judge_output"], 60), f["actual"], _short(f["decision"]["reason"], 90)]
            for f in suite["fail_closed"]
        ]))
        out.append("")
        example = next((c for c in suite["input_cases"] if c["expected"] == "block" and c["correct"]), None)
        if example:
            out.append("### 2.4 Example: guardrail correctly blocking an unsafe request\n")
            out.append(f"- **Role:** `{example['role']}`\n- **Prompt:** \"{example['question']}\"")
            out.append("- **Internal structured verdict (logged, never shown to the user):**\n")
            out.append("```json\n" + json.dumps(example["decision"], indent=2, ensure_ascii=False) + "\n```")
            out.append(f"- **What the user sees:** \"{SAFE_REFUSAL}\"\n")

    # Heuristics
    out.append("## 3. Heuristic evaluations (deterministic, run first)\n")
    out.append(_table(["Check", "Applicable", "Passed", "Failed", "Pass rate", "Critical"], [
        [name, s["applicable"], s["passed"], s["failed"], f"{s['pass_rate']:.0%}", "yes" if name in t.CRITICAL_HEURISTICS else ""]
        for name, s in results["heuristics"]["summary"].items()
    ]))
    out.append("")
    failures = results["heuristics"]["failures"]
    if failures:
        out.append("Failures on the labeled set:\n")
        out.append(_table(["ID", "Check", "Detail", "Question"], [[f["id"], f["check"], f["detail"], _short(f["question"], 80)] for f in failures]))
        out.append("")
    calibration = results.get("calibration", {})
    if calibration.get("heuristics"):
        out.append("### 3.1 Calibration: known-bad responses the checks must catch\n")
        out.append(_table(["ID", "Bad response", "Expected to fail", "Detected failures", "Caught"], [
            [c["id"], c["description"], ", ".join(c["expected_failures"]), ", ".join(c["detected_failures"]), "yes" if c["caught"] else "**no**"]
            for c in calibration["heuristics"]
        ]))
        example = next((c for c in calibration["heuristics"] if c["caught"] and "no_unsupported_figures" in c["expected_failures"]), None)
        example = example or next((c for c in calibration["heuristics"] if c["caught"]), None)
        if example:
            out.append("\n### 3.2 Example: heuristic check correctly failing a bad response\n")
            out.append(f"- **Case:** {example['description']} (`{example['id']}`)")
            out.append(f"- **Response:** \"{_short(example['answer'], 300)}\"")
            for check, detail in example["details"].items():
                out.append(f"- **`{check}` → FAIL:** {detail}")
            out.append("")

    # RAGAS
    ragas = results.get("ragas")
    out.append("## 4. RAGAS metrics\n")
    if ragas is None:
        out.append("_Skipped._\n")
    else:
        out.append(_table(["Metric", "Aggregate", "Threshold"], [
            [m, "n/a" if v is None else f"{v:.3f}", f">= {t.RAGAS_THRESHOLDS[m]:.2f}"] for m, v in ragas["aggregate"].items()
        ]))
        out.append(f"\nEvaluated {ragas['evaluated']} answerable questions (refusal / blocked items are excluded).\n")
        questions = {r["id"]: r["question"] for r in records}
        out.append(_table(["ID", "Question", "Faithfulness", "Answer relevancy", "Context precision", "Context recall"], [
            [row["id"], _short(questions.get(row["id"], ""), 70)] + ["n/a" if row[m] is None else f"{row[m]:.2f}"
             for m in ("faithfulness", "answer_relevancy", "context_precision", "context_recall")]
            for row in ragas["per_item"]
        ]))
        out.append("")

    # Judge
    judge = results.get("judge")
    out.append("## 5. LLM-as-a-judge\n")
    if judge is None:
        out.append("_Skipped._\n")
    else:
        s = judge["summary"]
        out.append(f"Judge model: `{meta['judge_model']}` (separate from the chatbot model `{meta['chat_model']}`). "
                   f"Mean overall **{s['mean_overall']:.2f}/5**, pass rate **{s['pass_rate']:.0%}**, errors {s['errors']}.\n")
        out.append(_table(["Criterion", "Mean (1-5)"], [
            [c, s[f"mean_{c}"]] for c in ("accuracy", "completeness", "refusal_appropriateness", "citation_correctness")
        ]))
        out.append("")
        out.append(_table(["ID", "Acc", "Comp", "Refusal", "Cite", "Overall", "Pass", "Justification"], [
            [r["id"], r.get("accuracy"), r.get("completeness"), r.get("refusal_appropriateness"), r.get("citation_correctness"),
             r.get("overall"), "yes" if r["passed"] else "**no**", _short(r.get("justification") or r.get("error", ""), 220)]
            for r in judge["per_item"]
        ]))
        out.append("")
        if calibration.get("judge"):
            out.append("### 5.1 Calibration: does the judge catch confident but wrong answers?\n")
            out.append(_table(["ID", "Bad response", "Should fail", "Judge overall", "Judge passed", "Justification"], [
                [r["id"], r["description"], "yes" if r["judge_should_fail"] else "no", r.get("overall"),
                 "yes" if r["passed"] else "no", _short(r.get("justification") or r.get("error", ""), 200)]
                for r in calibration["judge"]
            ]))
            out.append("")

    # Repeatability
    repeat = results.get("repeatability")
    if repeat:
        out.append("## 6. Repeatability\n")
        out.append(_table(["Metric"] + [f"Run {i + 1}" for i in range(len(repeat["runs"]))] + ["Max delta"], [
            [metric] + [run.get(metric) for run in repeat["runs"]] + [repeat["deltas"][metric]] for metric in repeat["deltas"]
        ]))
        out.append("")

    # Per-question trace index
    out.append("## 7. Per-question results and trace IDs\n")
    out.append("Each `request_id` is the LangSmith trace id and the key into `logs/requests.jsonl` / `GET /requests/{id}`.\n")
    out.append(_table(["ID", "Category", "Role", "Expected", "Guardrail", "Route", "Latency (ms)", "Tokens", "Request ID"], [
        [r["id"], r["category"], r["role"], r["expected_behavior"], r["guardrail"] + (f" ({r['block_category']})" if r.get("block_category") else ""),
         r["retrieval_type"], f"{r['latency_ms']:.0f}", r.get("tokens", {}).get("total_tokens", 0), f"`{r['request_id']}`"]
        for r in records
    ]))
    out.append("")
    return "\n".join(out)


def main() -> None:
    """Re-render a report from a saved ``results.json`` without calling any model.

    Example::

        uv run python -m evaluation.report evaluation/runs/<stamp>/results.json --run 1 --out evaluation/reports/sample_report.md
    """

    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Re-render an evaluation report from saved results.")
    parser.add_argument("results", type=Path)
    parser.add_argument("--run", type=int, help="Render only this repeat (1-based) from all_runs.")
    parser.add_argument("--no-suite", action="store_true", help="Omit the guardrail suite section.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    saved = json.loads(args.results.read_text(encoding="utf-8"))
    results = dict(saved)
    if args.run:
        results.update(saved["all_runs"][args.run - 1])
        results["repeatability"] = None
        results["meta"] = {**saved["meta"], "repeats": f"run {args.run} of {saved['meta']['repeats']}"}
    if args.no_suite:
        results.pop("guardrail_suite", None)
    results.pop("all_runs", None)
    results["verdict"] = compute_verdict(results)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_markdown(results), encoding="utf-8")
    print(f"{results['verdict']['overall']}: {', '.join(results['verdict']['failed_checks']) or 'all checks passed'} -> {args.out}")


if __name__ == "__main__":
    main()

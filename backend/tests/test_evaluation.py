"""Offline tests for the evaluation pipeline: datasets, heuristics, judge, report."""

import pytest

from evaluation import dataset, heuristics, report, thresholds
from evaluation.guardrail_suite import FAIL_CLOSED_PROBES, run_fail_closed_probes
from evaluation.judge import JudgeScores, LLMJudge, summarise
from mediassist.guardrails import SAFE_REFUSAL


def _record(**overrides):
    base = {
        "id": "x1",
        "question": "How often is a CVC dressing changed?",
        "role": "nurse",
        "expected_behavior": "answer",
        "ground_truth": "Every 72 hours.",
        "answer": "Every 72 hours.",
        "sources": [{"source_document": "icu_nursing_procedures.pdf", "section_title": "CVC", "collection": "nursing"}],
        "contexts": ["Dressing change every 72 hours."],
        "retrieval_type": "hybrid_rag",
        "guardrail": "allowed",
        "block_category": None,
        "latency_ms": 1000.0,
    }
    base.update(overrides)
    return base


def _results(record, check):
    return {r.check: r for r in heuristics.evaluate_record(record, 20000)}[check]


# --- datasets -----------------------------------------------------------------


def test_eval_set_is_large_and_valid():
    items = dataset.load_eval_set()

    assert len(items) >= 15
    behaviours = {item["expected_behavior"] for item in items}
    assert behaviours == {"answer", "refuse", "block"}
    assert sum(1 for item in items if item["category"] in {"adversarial", "hard"}) >= 5


def test_validate_rejects_bad_items():
    with pytest.raises(ValueError):
        dataset.validate_eval_items([{"id": "a"}])
    good = dataset.load_eval_set()[0]
    with pytest.raises(ValueError):
        dataset.validate_eval_items([good, good])


def test_calibration_and_guardrail_cases_load():
    assert len(dataset.load_calibration_set()) >= 5
    assert any(case["expected"] == "allow" for case in dataset.load_guardrail_cases())
    assert any(case["expected"] == "block" for case in dataset.load_output_guardrail_cases())


# --- heuristics ---------------------------------------------------------------


def test_good_record_passes_every_applicable_check():
    results = heuristics.evaluate_record(_record(), 20000)

    assert all(r.passed for r in results if r.applicable)


@pytest.mark.parametrize(
    "overrides,check",
    [
        ({"answer": ""}, "non_empty_answer"),
        ({"answer": None}, "non_empty_answer"),
        ({"sources": []}, "has_citation"),
        ({"latency_ms": 99999}, "latency_under_threshold"),
        ({"sources": [{"source_document": "b.pdf", "section_title": "s", "collection": "billing"}]}, "sources_within_role"),
        ({"answer": "Call the patient on 9876543210"}, "no_pii_in_answer"),
        ({"answer": "Change it every 72 hours with 500 mg chlorhexidine"}, "no_unsupported_figures"),
        ({"expected_behavior": "refuse", "answer": "Here is the billing package rate."}, "restricted_query_refused"),
        ({"expected_behavior": "block", "guardrail": "allowed"}, "adversarial_blocked"),
        ({"expected_behavior": "block", "guardrail": "blocked", "answer": "Blocked: prompt injection detected."}, "no_block_reason_leak"),
    ],
)
def test_each_heuristic_fails_bad_response(overrides, check):
    result = _results(_record(**overrides), check)

    assert result.applicable and not result.passed


def test_blocked_answer_with_safe_refusal_passes_block_checks():
    record = _record(expected_behavior="block", guardrail="blocked", answer=SAFE_REFUSAL, sources=[], retrieval_type="none")

    assert _results(record, "adversarial_blocked").passed
    assert _results(record, "no_block_reason_leak").passed


def test_refusal_passes_restricted_query_check():
    record = _record(expected_behavior="refuse", answer=SAFE_REFUSAL, sources=[])

    assert _results(record, "restricted_query_refused").passed


def test_run_heuristics_summary_and_fail_fast():
    summary = heuristics.run_heuristics([_record(), _record(id="x2", answer="")], 20000)

    assert summary["summary"]["non_empty_answer"] == {"applicable": 2, "passed": 1, "failed": 1, "pass_rate": 0.5}
    assert heuristics.fail_fast_reasons(summary) == ["non_empty_answer: 1 failure(s)"]
    assert heuristics.fail_fast_reasons(heuristics.run_heuristics([_record()], 20000)) == []


def test_calibration_set_is_fully_caught():
    outcomes = heuristics.run_calibration(dataset.load_calibration_set(), 20000)

    assert all(outcome["caught"] for outcome in outcomes), [o for o in outcomes if not o["caught"]]


# --- judge --------------------------------------------------------------------


def test_judge_scores_structured_output():
    scores = JudgeScores(accuracy=5, completeness=4, refusal_appropriateness=5, citation_correctness=4, justification="Good.")
    judge = LLMJudge(lambda _prompt: scores, "test-judge")

    result = judge.judge(_record())

    assert result["overall"] == 4.5 and result["passed"]
    assert result["justification"] == "Good."


def test_judge_low_accuracy_fails_even_with_high_average():
    scores = JudgeScores(accuracy=2, completeness=5, refusal_appropriateness=5, citation_correctness=5, justification="Wrong dose.")

    assert not LLMJudge(lambda _p: scores, "j").judge(_record())["passed"]


@pytest.mark.parametrize("raw", [{"accuracy": 9}, "not json", None])
def test_judge_malformed_output_counts_as_failure(raw):
    result = LLMJudge(lambda _p: raw, "j").judge(_record())

    assert result["overall"] is None and not result["passed"] and result["error"]


def test_judge_exception_counts_as_failure():
    def boom(_prompt):
        raise TimeoutError("slow")

    result = LLMJudge(boom, "j").judge(_record())

    assert not result["passed"] and "TimeoutError" in result["error"]


def test_judge_summary():
    rows = [
        {"id": "a", "overall": 4.0, "passed": True, "accuracy": 4, "completeness": 4, "refusal_appropriateness": 4, "citation_correctness": 4},
        {"id": "b", "overall": None, "passed": False, "error": "x"},
    ]

    summary = summarise(rows)

    assert summary["errors"] == 1 and summary["pass_rate"] == 0.5 and summary["mean_overall"] == 4.0


# --- guardrail suite fail-closed probes ---------------------------------------


def test_fail_closed_probes_all_block(event_logger):
    from mediassist.config import get_settings

    results = run_fail_closed_probes(get_settings(), event_logger)

    assert len(results) == len(FAIL_CLOSED_PROBES)
    assert all(row["correct"] for row in results)


# --- report -------------------------------------------------------------------


def _full_results(ragas_value=0.9, judge_mean=4.5, attack_rate=1.0):
    records = [_record(request_id="r1", category="normal", tokens={"total_tokens": 10})]
    return {
        "records": records,
        "heuristics": heuristics.run_heuristics(records, 20000),
        "calibration": {"heuristics": heuristics.run_calibration(dataset.load_calibration_set(), 20000)},
        "fail_fast_reasons": [],
        "ragas": {"aggregate": {m: ragas_value for m in thresholds.RAGAS_THRESHOLDS}, "per_item": [], "evaluated": 1},
        "judge": {"per_item": [], "summary": {"mean_overall": judge_mean, "pass_rate": 1.0, "errors": 0, "items": 1, "scored": 1,
                                              "mean_accuracy": 5, "mean_completeness": 5, "mean_refusal_appropriateness": 5,
                                              "mean_citation_correctness": 5}},
        "guardrail_suite": {"summary": {"attacks": 10, "attacks_blocked": int(10 * attack_rate), "attack_block_rate": attack_rate,
                                        "benign": 5, "benign_blocked": 0, "false_block_rate": 0.0,
                                        "fail_closed_probes": 7, "fail_closed_correct": 7},
                            "input_cases": [], "output_cases": [], "fail_closed": []},
        "repeatability": None,
        "meta": {"started_at": "now", "chat_model": "chat", "judge_model": "judge", "questions": 1, "repeats": 1, "duration_s": 1.0},
    }


def test_report_passes_when_all_thresholds_met():
    results = _full_results()

    verdict = report.compute_verdict(results)

    assert verdict["overall"] == "PASS", verdict["failed_checks"]


def test_report_fails_and_names_failed_checks():
    results = _full_results(ragas_value=0.2, judge_mean=2.0, attack_rate=0.8)

    verdict = report.compute_verdict(results)

    assert verdict["overall"] == "FAIL"
    assert {"ragas:faithfulness", "judge:mean_overall", "guardrail:attack_block_rate"} <= set(verdict["failed_checks"])


def test_fail_fast_forces_fail():
    results = _full_results()
    results["fail_fast_reasons"] = ["non_empty_answer: 3 failure(s)"]

    assert report.compute_verdict(results)["overall"] == "FAIL"


def test_markdown_report_contains_every_section():
    results = _full_results()
    results["verdict"] = report.compute_verdict(results)

    markdown = report.render_markdown(results)

    for heading in ("Overall verdict", "Scorecard", "Guardrails", "Heuristic evaluations", "RAGAS metrics", "LLM-as-a-judge",
                    "Example: heuristic check correctly failing"):
        assert heading in markdown

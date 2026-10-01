"""Load and validate the labeled evaluation datasets."""

import json
from pathlib import Path
from typing import Any

DATASET_DIR = Path(__file__).resolve().parent / "datasets"
EVAL_SET = DATASET_DIR / "eval_set.jsonl"
CALIBRATION_SET = DATASET_DIR / "calibration_set.jsonl"
GUARDRAIL_CASES = DATASET_DIR / "guardrail_cases.jsonl"
OUTPUT_GUARDRAIL_CASES = DATASET_DIR / "output_guardrail_cases.jsonl"

ROLES = {"doctor", "nurse", "billing_executive", "technician", "admin"}
BEHAVIOURS = {"answer", "refuse", "block"}
EVAL_FIELDS = {"id", "category", "role", "expected_behavior", "question", "ground_truth", "expected_sources"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSON Lines file, skipping blank lines."""

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def validate_eval_items(items: list[dict[str, Any]]) -> None:
    """Raise ``ValueError`` if the labeled set is malformed."""

    ids = [item.get("id") for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate ids in evaluation set.")
    for item in items:
        missing = EVAL_FIELDS - item.keys()
        if missing:
            raise ValueError(f"{item.get('id')}: missing fields {sorted(missing)}")
        if item["role"] not in ROLES:
            raise ValueError(f"{item['id']}: unknown role {item['role']!r}")
        if item["expected_behavior"] not in BEHAVIOURS:
            raise ValueError(f"{item['id']}: unknown expected_behavior {item['expected_behavior']!r}")


def load_eval_set(path: Path = EVAL_SET) -> list[dict[str, Any]]:
    items = load_jsonl(path)
    validate_eval_items(items)
    return items


def load_calibration_set(path: Path = CALIBRATION_SET) -> list[dict[str, Any]]:
    return load_jsonl(path)


def load_guardrail_cases(path: Path = GUARDRAIL_CASES) -> list[dict[str, Any]]:
    return load_jsonl(path)


def load_output_guardrail_cases(path: Path = OUTPUT_GUARDRAIL_CASES) -> list[dict[str, Any]]:
    return load_jsonl(path)

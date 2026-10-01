"""Structured logging, per-request metrics, and LangSmith helpers.

Every request produces two kinds of JSON Lines records under ``LOG_DIR``:

* ``guardrail_events.jsonl`` - one line per guardrail check (allowed or blocked, and why).
* ``requests.jsonl`` - one line per request: what was retrieved, the prompt that reached
  the LLM, the raw and final answer, stage latencies, and token usage.

Both files are append-only and queryable with ``jq``, pandas, or ``scripts/query_logs.py``.
LangSmith tracing is enabled purely through the standard ``LANGSMITH_*`` environment
variables; when it is off, the ``traceable`` decorators are no-ops.
"""

import json
import logging
import statistics
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("mediassist.observability")

GUARDRAIL_EVENTS_FILE = "guardrail_events.jsonl"
REQUESTS_FILE = "requests.jsonl"

# Per-request accumulators. ContextVars keep concurrent FastAPI requests apart.
_stage_timings: ContextVar[dict[str, float] | None] = ContextVar("stage_timings", default=None)
_token_usage: ContextVar[dict[str, dict[str, int]] | None] = ContextVar("token_usage", default=None)


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat()


@contextmanager
def request_scope() -> Iterator[tuple[dict[str, float], dict[str, dict[str, int]]]]:
    """Collect stage timings and token usage for the duration of one request."""

    timings: dict[str, float] = {}
    usage: dict[str, dict[str, int]] = {}
    timing_token = _stage_timings.set(timings)
    usage_token = _token_usage.set(usage)
    try:
        yield timings, usage
    finally:
        _stage_timings.reset(timing_token)
        _token_usage.reset(usage_token)


@contextmanager
def stage_timer(stage: str) -> Iterator[None]:
    """Add the elapsed milliseconds of a block to the current request's timings."""

    start = time.perf_counter()
    try:
        yield
    finally:
        timings = _stage_timings.get()
        if timings is not None:
            timings[stage] = round(timings.get(stage, 0.0) + (time.perf_counter() - start) * 1000, 2)


def record_usage(component: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
    """Accumulate token usage for one component (generation, guardrail, ...)."""

    usage = _token_usage.get()
    if usage is None:
        return
    bucket = usage.setdefault(component, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    bucket["prompt_tokens"] += int(prompt_tokens or 0)
    bucket["completion_tokens"] += int(completion_tokens or 0)
    bucket["total_tokens"] = bucket["prompt_tokens"] + bucket["completion_tokens"]


def record_langchain_usage(component: str, usage_by_model: dict[str, Any]) -> None:
    """Record usage collected by LangChain's ``get_usage_metadata_callback``."""

    for usage in usage_by_model.values():
        record_usage(component, usage.get("input_tokens", 0), usage.get("output_tokens", 0))


def total_tokens(usage: dict[str, dict[str, int]]) -> dict[str, int]:
    """Sum token usage across components."""

    return {
        key: sum(bucket.get(key, 0) for bucket in usage.values())
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def current_trace_id() -> str | None:
    """Return the active LangSmith trace id, or ``None`` when tracing is off."""

    try:
        from langsmith.run_helpers import get_current_run_tree

        run_tree = get_current_run_tree()
    except Exception:
        return None
    return str(run_tree.trace_id) if run_tree is not None else None


def add_trace_metadata(metadata: dict[str, Any]) -> None:
    """Attach metadata (for example token usage) to the active LangSmith run."""

    try:
        from langsmith.run_helpers import get_current_run_tree

        run_tree = get_current_run_tree()
        if run_tree is not None:
            run_tree.add_metadata(metadata)
    except Exception:
        pass


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(percentile / 100 * (len(ordered) - 1))))
    return round(ordered[index], 2)


class EventLogger:
    """Append-only JSON Lines writer and reader for guardrail events and request records."""

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self._lock = threading.Lock()

    @property
    def guardrail_path(self) -> Path:
        return self.log_dir / GUARDRAIL_EVENTS_FILE

    @property
    def requests_path(self) -> Path:
        return self.log_dir / REQUESTS_FILE

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        logger.info(line)

    def log_guardrail(self, record: dict[str, Any]) -> None:
        """Write one structured guardrail decision."""

        self._append(self.guardrail_path, {"event": "guardrail_decision", "timestamp": utc_now(), **record})

    def log_request(self, record: dict[str, Any]) -> None:
        """Write one structured request record including latency and token metrics."""

        self._append(self.requests_path, {"event": "chat_request", "timestamp": utc_now(), **record})

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def guardrail_events(self) -> list[dict[str, Any]]:
        return self._read(self.guardrail_path)

    def requests(self) -> list[dict[str, Any]]:
        return self._read(self.requests_path)

    def find_request(self, request_id: str) -> dict[str, Any] | None:
        """Reconstruct one request: its record plus every guardrail decision it produced."""

        record = next((item for item in reversed(self.requests()) if item.get("request_id") == request_id), None)
        if record is None:
            return None
        events = [event for event in self.guardrail_events() if event.get("request_id") == request_id]
        return {"request": record, "guardrail_events": events}

    def metrics(self) -> dict[str, Any]:
        """Aggregate the logs into basic, queryable service metrics."""

        requests = self.requests()
        events = self.guardrail_events()
        latencies = [float(item.get("latency_ms", {}).get("total", 0.0)) for item in requests]
        tokens = [int(item.get("tokens", {}).get("total_tokens", 0)) for item in requests]
        by_stage: dict[str, Counter] = {}
        for event in events:
            by_stage.setdefault(event.get("stage", "unknown"), Counter())[event.get("verdict", "unknown")] += 1
        block_categories = Counter(event.get("category", "unknown") for event in events if event.get("verdict") == "block")
        return {
            "requests": len(requests),
            "blocked_requests": sum(1 for item in requests if item.get("blocked")),
            "guardrail_decisions": {stage: dict(counts) for stage, counts in by_stage.items()},
            "block_categories": dict(block_categories),
            "latency_ms": {
                "mean": round(statistics.fmean(latencies), 2) if latencies else 0.0,
                "p50": _percentile(latencies, 50),
                "p95": _percentile(latencies, 95),
                "max": round(max(latencies), 2) if latencies else 0.0,
            },
            "tokens": {"total": sum(tokens), "mean_per_request": round(statistics.fmean(tokens), 2) if tokens else 0.0},
        }

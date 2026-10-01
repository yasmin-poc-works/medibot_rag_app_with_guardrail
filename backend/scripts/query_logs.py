"""Query MediBot's structured JSONL logs from the command line.

Usage (from ``backend/``)::

    uv run python scripts/query_logs.py metrics
    uv run python scripts/query_logs.py blocked --limit 10
    uv run python scripts/query_logs.py show <request_id>
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mediassist.observability import EventLogger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Query MediBot guardrail and request logs.")
    parser.add_argument("--log-dir", type=Path, default=Path(__file__).resolve().parents[1] / "logs")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("metrics", help="Aggregate allow/block counts, latency and tokens.")
    blocked = commands.add_parser("blocked", help="List the most recent blocked requests.")
    blocked.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show", help="Reconstruct one request with every guardrail decision.")
    show.add_argument("request_id")
    args = parser.parse_args()

    events = EventLogger(args.log_dir)
    if args.command == "metrics":
        print(json.dumps(events.metrics(), indent=2))
    elif args.command == "blocked":
        rows = [r for r in events.requests() if r.get("blocked")][-args.limit:]
        for row in rows:
            print(f"{row['timestamp']}  {row['request_id']}  {row['role']:<17} {row.get('blocked_stage')}/{row.get('block_category'):<24} {row['question'][:80]}")
    else:
        detail = events.find_request(args.request_id)
        if detail is None:
            print(f"No request with id {args.request_id}", file=sys.stderr)
            return 1
        print(json.dumps(detail, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

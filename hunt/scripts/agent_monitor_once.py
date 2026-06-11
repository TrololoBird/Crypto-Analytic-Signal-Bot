#!/usr/bin/env python3
"""One-shot hunt monitor: verify_diff + exit 2 on mismatch (for agent repair loop)."""

from __future__ import annotations

import argparse
from pathlib import Path

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.monitor import run_verify_sync


def main() -> int:
    parser = argparse.ArgumentParser(description="Hunt watch verify pass (agent monitor)")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--session-dir", type=Path, default=None)
    args = parser.parse_args()

    result = run_verify_sync(limit=args.limit, session_dir=args.session_dir)
    print(result["table"])
    print()
    print(f"mismatches: {result['mismatch_count']}")
    if result["alert_path"]:
        print(f"alert: {result['alert_path']}")
    return 2 if result["mismatch_count"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())

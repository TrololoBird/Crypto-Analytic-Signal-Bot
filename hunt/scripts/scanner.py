#!/usr/bin/env python3
"""CLI: scan all USD-M tickers for pump/dump hunt candidates."""

from __future__ import annotations

import argparse
import asyncio
import json

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.scanner_runner import run_scan
from hunt_watch.screener import HUNT_SCORE_WATCH_THRESHOLD
from hunt_watch.scriptutil import configure_script_logging

from hunt_core.errors import DEFENSIVE_EXC

LOG = configure_script_logging("hunt_watch.scanner")


def main() -> None:
    parser = argparse.ArgumentParser(description="Hunt pump/dump scanner")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--min-score", type=float, default=HUNT_SCORE_WATCH_THRESHOLD)
    parser.add_argument("--print", action="store_true", help="Print JSON summary to stdout")
    args = parser.parse_args()
    try:
        summary = asyncio.run(run_scan(limit=args.limit, min_score=args.min_score))
    except DEFENSIVE_EXC:
        LOG.exception("hunt_scan_failed")
        raise
    if args.print:
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

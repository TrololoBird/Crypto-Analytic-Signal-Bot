#!/usr/bin/env python3
"""Rotate hunt tick JSONL — daily files, gzip archive, 14-day retention."""

from __future__ import annotations

import argparse

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.tick_rotate import RETENTION_DAYS, rotate_hunt_ticks


def main() -> int:
    parser = argparse.ArgumentParser(description="Rotate hunt tick JSONL")
    parser.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = rotate_hunt_ticks(retention_days=args.retention_days, dry_run=args.dry_run)
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

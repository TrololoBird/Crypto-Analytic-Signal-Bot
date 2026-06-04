#!/usr/bin/env python3
"""Summarize supervised live_watch rollups and optional DB outcomes."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

try:
    from scripts.common import bootstrap_repo_path
except ModuleNotFoundError:
    from common import bootstrap_repo_path

bootstrap_repo_path()

from bot.diagnostics.live_watch import find_latest_rollup, summarize_rollup
from bot.domain.config import load_settings
from bot.persistence.db_status import collect_db_status

LOG = logging.getLogger("scripts.live_watch_rollup_report")


async def build_report(*, rollup_path: Path, config: Path) -> dict[str, object]:
    rollup = summarize_rollup(rollup_path)
    settings = load_settings(config)
    db_status = await collect_db_status(settings)
    rollup["db_status"] = {
        "db_path": str(settings.db_path),
        "migration_version": db_status.migration_version,
        "outcomes_total": db_status.outcomes_total,
        "signal_counts": dict(db_status.signal_counts),
        "outcome_counts": dict(db_status.outcome_counts),
    }
    return rollup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--rollup", type=Path, default=None, help="rollup_*.json path")
    parser.add_argument(
        "--live-watch-dir",
        type=Path,
        default=Path("data/live_watch"),
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    rollup_path = args.rollup
    if rollup_path is None:
        rollup_path = find_latest_rollup(args.live_watch_dir)
    if rollup_path is None or not rollup_path.is_file():
        LOG.error("no rollup found under %s", args.live_watch_dir)
        return 1

    report = asyncio.run(build_report(rollup_path=rollup_path, config=args.config))
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        LOG.info("report written | path=%s", args.output)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

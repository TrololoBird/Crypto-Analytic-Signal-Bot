#!/usr/bin/env python3
"""Wave I: run calibration pipeline after a supervised live_watch session."""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

try:
    from scripts.common import bootstrap_repo_path
except ModuleNotFoundError:
    from common import bootstrap_repo_path

bootstrap_repo_path()

from scripts.calibration_pipeline import run_calibration_pipeline

LOG = logging.getLogger("scripts.post_session_calibration")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--run-id", type=str, required=True)
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("data/bot/reports"),
    )
    parser.add_argument(
        "--live-watch-dir",
        type=Path,
        default=Path("data/live_watch"),
    )
    parser.add_argument(
        "--rollup",
        action="store_true",
        help="Also write live_watch_rollup_report when rollup exists",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    if args.rollup:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/live_watch_rollup_report.py",
                "--config",
                str(args.config),
                "--live-watch-dir",
                str(args.live_watch_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            LOG.warning("rollup report skipped | stderr=%s", (proc.stderr or "")[-300:])

    summary = asyncio.run(
        run_calibration_pipeline(
            config=args.config,
            reports_dir=args.reports_dir / args.run_id.strip(),
            static_matrix=False,
            run_id=args.run_id.strip(),
            live_watch_dir=args.live_watch_dir,
        )
    )
    zero = summary.get("matrix", {}).get("zero_hit_triage")
    zero_n = summary.get("matrix", {}).get("zero_run_count")
    LOG.info(
        "post_session_calibration complete | run_id=%s zero_hit=%s zero_count=%s",
        args.run_id,
        zero,
        zero_n,
    )
    return int(summary.get("matrix", {}).get("exit_code", 0))


if __name__ == "__main__":
    raise SystemExit(main())

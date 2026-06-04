#!/usr/bin/env python3
"""Nightly strategy calibration report — wraps shortlist matrix for ops."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

try:
    from scripts.common import bootstrap_repo_path
except ModuleNotFoundError:
    from common import bootstrap_repo_path

bootstrap_repo_path()

LOG = logging.getLogger("scripts.nightly_strategy_calibration")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--symbols", type=int, default=25)
    parser.add_argument("--output", type=Path, default=Path("data/bot/reports/nightly_calibration.json"))
    parser.add_argument(
        "--no-include-basis",
        action="store_true",
        help="Skip basis REST warmup during live shortlist calibration",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "scripts/strategy_shortlist_matrix.py",
        "--config",
        str(args.config),
        "--live-shortlist",
        "--json",
    ]
    if not args.no_include_basis:
        cmd.append("--include-basis")
        cmd.extend(["--basis-warm-limit", str(args.symbols)])
    LOG.info("running calibration matrix | cmd=%s", " ".join(cmd))
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        LOG.error("calibration matrix failed | exit=%s stderr=%s", proc.returncode, (proc.stderr or "")[:300])
        return proc.returncode
    if proc.stdout.strip():
        args.output.write_text(proc.stdout, encoding="utf-8")
    LOG.info("calibration report written | path=%s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())

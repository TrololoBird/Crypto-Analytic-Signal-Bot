#!/usr/bin/env python3
"""Supervised research harvest — deep capture on ~10 symbols (no calibration, no Telegram)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    from scripts.common import bootstrap_repo_path, configure_script_logging
except ModuleNotFoundError:  # pragma: no cover
    from common import bootstrap_repo_path, configure_script_logging

ROOT = Path(__file__).resolve().parents[1]
LOG = configure_script_logging("scripts.research_harvest_session")


def main() -> int:
    bootstrap_repo_path()
    parser = argparse.ArgumentParser(description="Run research harvest capture session")
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--symbols", nargs="*", default=())
    parser.add_argument(
        "--skip-clean",
        action="store_true",
        help="Do not wipe smoke telemetry before harvest",
    )
    args = parser.parse_args()

    if not args.skip_clean:
        clean = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "clean_session_data.py"),
                "--mode",
                "smoke",
                "--config",
                str(args.config),
            ],
            cwd=str(ROOT),
            check=False,
        )
        if clean.returncode != 0:
            LOG.warning("session_clean_failed", exit_code=clean.returncode)

    cmd = [
        sys.executable,
        str(ROOT / "main.py"),
        "harvest",
        "--config",
        str(args.config),
        "--minutes",
        str(max(0.0, args.minutes)),
    ]
    for symbol in args.symbols:
        cmd.extend(["--symbols", str(symbol).strip().upper()])

    LOG.info("launching_harvest", command=" ".join(cmd))
    completed = subprocess.run(cmd, cwd=str(ROOT), check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

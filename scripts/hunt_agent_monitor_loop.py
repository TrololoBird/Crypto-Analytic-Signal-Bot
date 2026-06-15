#!/usr/bin/env python3
"""Agent-owned hunt process monitor — watch supervisor health (verify removed in P1)."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LOG = logging.getLogger("hunt_agent_monitor")


def _watch_alive() -> tuple[bool, str]:
    r = subprocess.run(
        ["pgrep", "-fl", "hunt_core.* watch"],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (r.stdout or "").strip()
    return bool(out), out or "no watch process"


def main() -> int:
    parser = argparse.ArgumentParser(description="Hunt agent monitor loop (process health)")
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--interval", type=int, default=600, help="Seconds between health passes")
    parser.add_argument("--limit", type=int, default=15, help="Ignored (compat with old CLI)")
    args = parser.parse_args()
    _ = args.limit

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(ROOT / "logs" / "hunt_agent_monitor_loop.log", encoding="utf-8"),
        ],
    )

    end_at = time.time() + args.hours * 3600.0
    passes = 0

    while time.time() < end_at:
        passes += 1
        alive, detail = _watch_alive()
        LOG.info("monitor_pass n=%s alive=%s detail=%s", passes, alive, detail[:120] or "—")
        if not alive:
            LOG.warning("NEEDS_FIX watch process not running")

        sleep_s = min(float(args.interval), max(1.0, end_at - time.time()))
        time.sleep(sleep_s)

    LOG.info("monitor_loop_done passes=%s", passes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

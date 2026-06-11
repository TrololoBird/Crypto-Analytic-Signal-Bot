#!/usr/bin/env python3
"""Agent-owned hunt monitor loop — verify every N min, log alerts for repair.

Runs alongside supervised_session; use when agent must actively track mismatches.
Exit 2 on mismatch so orchestrators can trigger fix+restart.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hunt"))

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.monitor import run_verify_sync

LOG = logging.getLogger("hunt_agent_monitor")


def main() -> int:
    parser = argparse.ArgumentParser(description="Hunt agent monitor loop")
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--interval", type=int, default=600, help="Seconds between verify passes")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()

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
    max_mismatches = 0

    while time.time() < end_at:
        passes += 1
        LOG.info("monitor_pass start n=%s", passes)
        result = run_verify_sync(limit=args.limit)
        mc = int(result["mismatch_count"])
        max_mismatches = max(max_mismatches, mc)
        LOG.info("monitor_pass done mismatches=%s severe=%s", mc, result["severe_count"])
        if mc > 0:
            LOG.warning("NEEDS_FIX alert=%s", result["alert_path"])
            for row in result["mismatches"]:
                LOG.warning(
                    "  %s %s bot=%s/%s ind=%s",
                    row.symbol,
                    row.verdict,
                    row.bot_phase,
                    row.bot_bias,
                    row.ind_bias,
                )
        else:
            LOG.info("monitor_pass clean")

        sleep_s = min(float(args.interval), max(1.0, end_at - time.time()))
        time.sleep(sleep_s)

    LOG.info("monitor_loop_done passes=%s max_mismatches=%s", passes, max_mismatches)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

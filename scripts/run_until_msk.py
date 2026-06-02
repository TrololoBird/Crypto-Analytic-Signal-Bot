"""Run the bot until a target Moscow time, then stop gracefully."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


def _parse_target(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            parsed = None
    if parsed is None:
        raise argparse.ArgumentTypeError(f"invalid time: {value!r}")
    now_msk = datetime.now(MSK)
    if fmt == "%H:%M":
        target = now_msk.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        if target <= now_msk:
            target = target.replace(day=target.day + 1)
    else:
        target = parsed.replace(tzinfo=MSK)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--until",
        default="10:30",
        help="Stop time in MSK (default: 10:30 today)",
    )
    args = parser.parse_args()
    target = _parse_target(args.until)
    print(f"Starting bot; will stop at {target.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    proc = subprocess.Popen([sys.executable, "main.py"], cwd=os.getcwd())
    try:
        while proc.poll() is None:
            if datetime.now(MSK) >= target:
                print("Target time reached; stopping bot...")
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return 0
            time.sleep(15)
        return proc.returncode or 0
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait(timeout=15)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

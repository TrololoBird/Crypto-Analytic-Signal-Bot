"""Smoke gate for ``python -m hunt_core watch --once`` (Phase 0)."""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time


def _kill_legacy_watchers() -> None:
    """Stop stale hunt_core watch holding watch.pid or Binance WS."""
    try:
        subprocess.run(
            ["pkill", "-f", "hunt_core.* watch"],
            check=False,
            capture_output=True,
        )
    except OSError:
        pass
    from hunt_core.paths import DATA

    lock = DATA / "watch.pid"
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


async def _run_once(symbols: tuple[str, ...], *, timeout_s: float) -> int:
    from hunt_core.bootstrap import bootstrap

    bootstrap()
    from hunt_core.runtime.cycle import run_loop

    try:
        await asyncio.wait_for(
            run_loop(symbols, 30, True, send_telegram=False),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        print(f"FAIL: watch --once exceeded {timeout_s}s", file=sys.stderr)
        return 1
    print(f"OK: watch --once completed for {symbols}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hunt watch --once smoke")
    parser.add_argument("symbols", nargs="*", default=["BTCUSDT"])
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--kill-legacy", action="store_true", default=True)
    parser.add_argument("--no-kill-legacy", action="store_false", dest="kill_legacy")
    args = parser.parse_args(argv)
    if args.kill_legacy:
        _kill_legacy_watchers()
        time.sleep(0.5)
    symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
    return asyncio.run(_run_once(symbols, timeout_s=args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())

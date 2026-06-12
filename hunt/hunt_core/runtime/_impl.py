#!/usr/bin/env python3
"""Hunter CLI — lock, signals, argparse (thin app shell)."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_core.runtime.cycle import run_loop
from hunt_core.runtime.settings import request_stop
from hunt_watch.targets import DEFAULT_SYMBOLS


def _on_signal(*_args: object) -> None:
    request_stop()


def _acquire_single_instance_lock() -> None:
    """Refuse to start if another live watcher holds the lock."""
    from hunt_watch.paths import DATA

    lock = DATA / "watch.pid"
    if lock.exists():
        try:
            other = int(lock.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            other = 0
        if other and other != os.getpid():
            alive = False
            try:
                os.kill(other, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True
            if alive:
                raise SystemExit(
                    f"watch.py already running (pid={other}); refusing to start a second writer. "
                    f"Kill it first or remove {lock} if stale."
                )
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal minute watch + Telegram")
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=list(DEFAULT_SYMBOLS),
        help="CLI extras on top of anchors BTC ETH XAU XAG + scanner watchlist",
    )
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-telegram", action="store_true", help="Log only, no Telegram sends")
    args = parser.parse_args()
    symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if not args.once:
        _acquire_single_instance_lock()
    asyncio.run(
        run_loop(
            symbols,
            args.interval,
            args.once,
            send_telegram=not args.no_telegram,
        )
    )


if __name__ == "__main__":
    main()

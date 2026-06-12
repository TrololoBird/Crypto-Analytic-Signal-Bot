#!/usr/bin/env python3
"""On-demand /signal probe — CLI mirror of hunt Telegram /signal command."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.scriptutil import configure_script_logging
from hunt_watch.symbol_probe import deliver_signal_probe, probe_symbol_signal

LOG = configure_script_logging("hunt.signal")


async def _main_async(symbol: str, *, telegram: bool, stagger_ms: int) -> int:
    if telegram:
        from hunt_core.domain.config import load_settings
        from hunt_core.telegram import TelegramBroadcaster

        settings = load_settings()
        if not settings.tg_token or not settings.target_chat_id:
            print("Telegram not configured", file=sys.stderr)
            return 1
        broadcaster = TelegramBroadcaster(settings.tg_token, settings.target_chat_id)
        try:
            row = await deliver_signal_probe(broadcaster, symbol, stagger_ms=stagger_ms)
        finally:
            await broadcaster.close()
    else:
        row = await probe_symbol_signal(symbol, stagger_ms=stagger_ms)
        print(json.dumps(row, indent=2, default=str))
    if row.get("error"):
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Hunt /signal — on-demand symbol probe")
    parser.add_argument("symbol", help="e.g. BEATUSDT or BEAT")
    parser.add_argument("--telegram", action="store_true", help="Send result to Telegram")
    parser.add_argument("--stagger-ms", type=int, default=150, help="REST delay between kline fetches")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main_async(args.symbol, telegram=args.telegram, stagger_ms=args.stagger_ms)))


if __name__ == "__main__":
    main()

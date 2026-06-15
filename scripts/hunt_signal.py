#!/usr/bin/env python3
"""Backward-compat — one-shot signal probe (``/signal`` parity)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunt"))

from hunt_core.bootstrap import bootstrap, require_feature_stack
from hunt_core.runtime.symbol_probe import deliver_signal_probe
from hunt_core.secrets import load_secrets


async def _run(symbol: str) -> int:
    from hunt_core.deliver.telegram import TelegramBroadcaster

    bootstrap()
    require_feature_stack()
    secrets = load_secrets()
    token, chat_id = secrets.tg_token, secrets.target_chat_id
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID required", file=sys.stderr)
        return 2
    broadcaster = TelegramBroadcaster(token, str(chat_id))
    try:
        await deliver_signal_probe(broadcaster, symbol)
    finally:
        await broadcaster.close()
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: hunt_signal.py SYMBOL", file=sys.stderr)
        return 2
    return asyncio.run(_run(sys.argv[1].upper()))


if __name__ == "__main__":
    raise SystemExit(main())

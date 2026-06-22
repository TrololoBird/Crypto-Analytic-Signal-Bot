"""Probe pinned SignalQueue TOP3 after live deep ticks."""
from __future__ import annotations

import asyncio
import json
import sys

from hunt_core.deep.verdict_v2.signal_queue import format_queue_telegram, load_signal_queue
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.domain.config import load_settings
from hunt_core.market.factory import create_hunt_market_plane_from_settings
from hunt_core.runtime.deep_assembly import assemble_deep_tick


async def _main() -> int:
    settings = load_settings()
    plane = await create_hunt_market_plane_from_settings(settings)
    client = plane.client
    for sym in PINNED_SYMBOLS:
        await assemble_deep_tick(sym, client, stagger_ms=60)
    await plane.close()
    queue = load_signal_queue()
    print(json.dumps(queue, indent=2, ensure_ascii=False))
    tg = format_queue_telegram(queue)
    if tg:
        print("\n--- TG preview ---\n", tg, sep="", file=sys.stderr)
    top3 = queue.get("top3") or []
    return 0 if top3 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

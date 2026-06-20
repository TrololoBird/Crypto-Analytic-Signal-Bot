"""Live smoke: HuntMarketPlane CCXT REST + Pro WS (no funding error spam)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys


async def _run(symbols: tuple[str, ...], *, ws_seconds: float) -> int:
    from hunt_core.bootstrap import bootstrap
    from hunt_core.domain.config import load_settings
    from hunt_core.market.cross import apply_cross_exchange_env, load_cross_exchange_config
    from hunt_core.market.factory import create_hunt_market_plane_from_settings

    bootstrap()
    logging.basicConfig(level=logging.INFO)
    apply_cross_exchange_env(load_cross_exchange_config())
    settings = load_settings()
    plane = await create_hunt_market_plane_from_settings(settings)
    try:
        await plane.client.load_markets()
        rows = await plane.client.fetch_ohlcv_list("BTCUSDT", "1m", limit=2)
        if len(rows) < 1:
            print("FAIL: REST OHLCV empty", file=sys.stderr)
            return 1
        await plane.streams.start()
        plane.streams.set_symbols(list(symbols))
        task_names = [t.get_name() for t in plane.streams._tasks]
        if "hunt_ccxt_funding" in task_names:
            print(
                f"FAIL: hunt_ccxt_funding started on Binance primary (tasks={task_names})",
                file=sys.stderr,
            )
            return 1
        await asyncio.sleep(ws_seconds)
        snap = plane.streams.snapshot(symbols[0])
        if not snap.get("ws_connected"):
            print("FAIL: ws_connected=False", file=sys.stderr)
            return 1
        if "hunt_ccxt_funding" in str(logging.getLogger().handlers):
            pass
        print(
            f"OK: plane REST+WS | markets={len(plane.client.exchange.markets)} "
            f"ws_connected={snap.get('ws_connected')} ws_base={snap.get('ws_base_url')}"
        )
        return 0
    finally:
        await plane.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CCXT market plane smoke")
    parser.add_argument("symbols", nargs="*", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--ws-seconds", type=float, default=3.0)
    args = parser.parse_args(argv)
    symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
    return asyncio.run(_run(symbols, ws_seconds=args.ws_seconds))


if __name__ == "__main__":
    raise SystemExit(main())

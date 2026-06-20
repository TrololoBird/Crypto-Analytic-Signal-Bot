"""Live smoke — maps integration on BTC/ETH probe (plan verification)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

_MAP_MARKET_KEYS = (
    "map_vp_poc",
    "map_sticky_wall_count",
    "liq_heatmap_nearest_long",
    "liq_heatmap_nearest_short",
    "liq_forward_confidence",
    "liq_forward_weight",
    "liq_cascade_risk",
)
_MAP_BUNDLE_KEYS = ("orderbook", "liquidation", "volume_profile")


def _check_row(row: dict[str, Any]) -> list[str]:
    fails: list[str] = []
    sym = row.get("symbol") or "?"
    if row.get("error"):
        fails.append(f"{sym}: probe error {row['error']}")
        return fails
    market = row.get("market") or {}
    maps = row.get("maps") or {}
    if not isinstance(maps, dict) or not maps:
        fails.append(f"{sym}: missing row['maps']")
    else:
        present = [k for k in _MAP_BUNDLE_KEYS if maps.get(k)]
        if not present:
            fails.append(f"{sym}: maps bundle empty ({list(maps.keys())})")
    if not isinstance(market, dict):
        fails.append(f"{sym}: missing market dict")
        return fails
    hits = [k for k in _MAP_MARKET_KEYS if market.get(k) is not None]
    if len(hits) < 2:
        fails.append(f"{sym}: too few map market keys ({hits})")
    price = float(row.get("price") or 0)
    if price <= 0:
        fails.append(f"{sym}: invalid price")
    return fails


async def _run(symbols: tuple[str, ...]) -> tuple[list[dict[str, Any]], list[str]]:
    from hunt_core.bootstrap import bootstrap

    bootstrap()
    from hunt_core.runtime.symbol_probe import probe_symbol_signal

    rows: list[dict[str, Any]] = []
    fails: list[str] = []
    for sym in symbols:
        row = await probe_symbol_signal(sym, auto_watchlist=False, stagger_ms=250)
        rows.append(row)
        fails.extend(_check_row(row))
    return rows, fails


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maps live integration smoke")
    parser.add_argument("symbols", nargs="*", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--dump", action="store_true")
    args = parser.parse_args(argv)
    symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
    rows, fails = asyncio.run(_run(symbols))
    if args.dump:
        compact = []
        for row in rows:
            mkt = row.get("market") or {}
            compact.append(
                {
                    "symbol": row.get("symbol"),
                    "price": row.get("price"),
                    "maps_keys": list((row.get("maps") or {}).keys()),
                    "map_market": {k: mkt.get(k) for k in _MAP_MARKET_KEYS if mkt.get(k) is not None},
                }
            )
        print(json.dumps(compact, ensure_ascii=False, indent=2, default=str))
    else:
        for row in rows:
            mkt = row.get("market") or {}
            mk = [k for k in _MAP_MARKET_KEYS if mkt.get(k) is not None]
            print(f"{row.get('symbol')} maps={list((row.get('maps') or {}).keys())} market_keys={mk}")
    if fails:
        for f in fails:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print(f"OK: maps integration on {len(rows)} symbols")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

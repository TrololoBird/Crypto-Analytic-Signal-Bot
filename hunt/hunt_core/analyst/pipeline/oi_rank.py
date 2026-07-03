from __future__ import annotations

import time
from typing import Any

import polars as pl

from hunt_core.analyst.pipeline._helpers import safe_float_opt

_cached_ranks: dict[str, tuple[int, float]] = {}
_cache_ts: float = 0.0
_CACHE_TTL: float = 300.0


def fetch_oi_rank(exchange: Any, symbol: str) -> int | None:
    global _cached_ranks, _cache_ts

    now = time.time()
    if _cached_ranks and (now - _cache_ts) < _CACHE_TTL:
        if symbol in _cached_ranks:
            return _cached_ranks[symbol][0]

    try:
        markets = exchange.fetchMarkets()
        perp_symbols = [
            m["symbol"] for m in markets
            if m.get("type") == "swap" and m.get("quote") == "USDT"
        ]

        oi_data: list[dict[str, Any]] = []
        for sym in perp_symbols:
            try:
                oi = exchange.fetchOpenInterest(sym)
                oi_val = safe_float_opt(oi.get("openInterest"))
                mark_price = safe_float_opt(oi.get("markPrice"))
                if oi_val is not None and mark_price is not None and oi_val > 0 and mark_price > 0:
                    oi_data.append({
                        "symbol": sym,
                        "oi_value_usd": oi_val * mark_price,
                    })
            except Exception:
                continue

        if not oi_data:
            return None

        df = pl.DataFrame(oi_data).sort("oi_value_usd", descending=True)
        df = df.with_columns(pl.int_range(1, pl.count() + 1).alias("oi_rank"))

        ranks: dict[str, int] = {}
        for row in df.iter_rows(named=True):
            ranks[row["symbol"]] = row["oi_rank"]

        _cached_ranks = {s: (r, now) for s, r in ranks.items()}
        _cache_ts = now

        return ranks.get(symbol)

    except Exception:
        if symbol in _cached_ranks:
            return _cached_ranks[symbol][0]
        return None


def fetch_oi_value(exchange: Any, symbol: str) -> float | None:
    try:
        oi = exchange.fetchOpenInterest(symbol)
        oi_val = safe_float_opt(oi.get("openInterest"))
        mark_price = safe_float_opt(oi.get("markPrice"))
        if oi_val is not None and mark_price is not None:
            return oi_val * mark_price
        return None
    except Exception:
        return None


def fetch_oi_change_24h(exchange: Any, symbol: str) -> float | None:
    try:
        now_oi = exchange.fetchOpenInterest(symbol)
        now_val = safe_float_opt(now_oi.get("openInterest"))

        since = exchange.milliseconds() - 86400 * 1000
        _ = exchange.fetchOpenInterestHistory(symbol, since=since, limit=2, timeframe="1h")
        return None
    except Exception:
        return None


def clear_oi_cache() -> None:
    global _cached_ranks, _cache_ts
    _cached_ranks.clear()
    _cache_ts = 0.0

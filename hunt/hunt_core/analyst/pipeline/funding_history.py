from __future__ import annotations

import time
from typing import Any

import polars as pl

from hunt_core.analyst.pipeline._helpers import safe_float_opt

_cached_percentiles: dict[str, tuple[float, float, float]] = {}
_cache_ts: float = 0.0
_CACHE_TTL: float = 3600.0


def fetch_funding_percentile(
    exchange: Any,
    symbol: str,
    min_points: int = 90,
    max_age_days: int = 90,
) -> float | None:
    global _cached_percentiles, _cache_ts

    now = time.time()
    cache_key = f"{symbol}:percentile"
    if cache_key in _cached_percentiles and (now - _cache_ts) < _CACHE_TTL:
        val, _, _ = _cached_percentiles[cache_key]
        return val

    try:
        since = exchange.milliseconds() - max_age_days * 86400 * 1000
        all_rates: list[float] = []
        current_rate: float | None = None

        while since < exchange.milliseconds():
            funding = exchange.fetchFundingRateHistory(
                symbol,
                since=since,
                limit=1000,
            )
            if not funding:
                break
            for entry in funding:
                rate = safe_float_opt(entry.get("fundingRate"))
                if rate is not None:
                    all_rates.append(rate)
            since = funding[-1]["timestamp"] + 1

        if not all_rates:
            return None

        current_rate = all_rates[-1]

        if len(all_rates) < min_points:
            return None

        df = pl.DataFrame({"rate": all_rates})
        percentile = df.select(
            (pl.col("rate") < current_rate).mean()
        ).item()

        _cached_percentiles[cache_key] = (percentile, now, float(len(all_rates)))
        _cache_ts = now
        return percentile

    except Exception:
        if cache_key in _cached_percentiles:
            return _cached_percentiles[cache_key][0]
        return None





def clear_funding_cache() -> None:
    global _cached_percentiles, _cache_ts
    _cached_percentiles.clear()
    _cache_ts = 0.0

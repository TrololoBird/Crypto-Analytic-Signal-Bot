"""FAPI ratio fetch helpers extracted from market/client.py (debloat)."""
from __future__ import annotations

import time
from typing import Any, Callable

import structlog

LOG = structlog.get_logger("hunt_core.market.fapi_ratio")


def safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def fetch_fapi_ratio_metric(
    *,
    symbol: str,
    period: str,
    fetcher: Callable[..., Any],
    ratio_keys: tuple[str, ...],
    cache: dict[tuple[str, str], tuple[float, float]],
    ttl_seconds: float,
    direct_fetch: Callable[[Callable[[], Any], str, int, str], Any],
    bin_sym: Callable[[str], str],
) -> float | None:
    """Shared cache+fetch for fapiData ratio endpoints (taker, top trader, etc.)."""
    sym = bin_sym(symbol)
    cache_key = (sym, period)
    now = time.monotonic()
    cached = cache.get(cache_key)
    if cached and (now - cached[0]) < ttl_seconds:
        return cached[1]
    try:
        payload = await direct_fetch(
            lambda: fetcher({"symbol": sym, "period": period, "limit": 1}),
            f"fapi_ratio:{sym}:{period}",
            1,
            getattr(fetcher, "__name__", "fapi_ratio"),
        )
        rows = payload if isinstance(payload, list) else [payload]
        if not rows:
            return None
        item = rows[-1] if isinstance(rows[-1], dict) else {}
        for key in ratio_keys:
            val = safe_float(item.get(key))
            if val > 0:
                cache[cache_key] = (now, val)
                return val
    except Exception as exc:
        LOG.warning("fetch_fapi_ratio_failed | sym=%s period=%s error=%s", sym, period, exc)
    return None


__all__ = ["fetch_fapi_ratio_metric", "safe_float"]

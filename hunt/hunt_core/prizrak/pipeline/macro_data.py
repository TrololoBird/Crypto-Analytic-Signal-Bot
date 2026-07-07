from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

from hunt_core.prizrak.pipeline._helpers import safe_float_opt
from hunt_core.prizrak.pipeline._rest_pace import pace


@dataclass
class MacroDataSnapshot:
    btc_d: float | None = None
    btc_d_change_24h: float | None = None
    total3_cap: float | None = None
    total3_change_24h: float | None = None
    timestamp: float = 0.0
    stale: bool = False


_cached: MacroDataSnapshot | None = None
_last_fetch: float = 0.0
_history: list[tuple[float, float, float]] = []
_HISTORY_MAX_AGE = 90_000.0


async def _compute_ccxt_proxy(exchange: Any, rest_gate: Any = None) -> tuple[float | None, float | None]:
    """BTC.D / TOTAL3 proxy via CCXT fetchTickers() quoteVolume shares (no external HTTP)."""
    try:
        await pace(rest_gate, weight=40, label="deep_macro_proxy:fetchTickers")
        tickers = await exchange.fetchTickers()
    except Exception:
        return None, None

    btc_vol: float | None = None
    total_vol = 0.0
    alt_vol = 0.0
    for symbol, t in tickers.items():
        # Binance USDⓈ-M linear perpetuals: "BASE/USDT:USDT" (dated futures have a
        # "-YYMMDD" suffix and BTCDOM-style index products must be excluded).
        if not symbol.endswith("/USDT:USDT"):
            continue
        base = symbol.split("/", 1)[0]
        if base in ("BTCDOM", "DEFI"):
            continue
        qv = safe_float_opt(t.get("quoteVolume"))
        if qv is None or qv <= 0:
            continue
        total_vol += qv
        if base == "BTC":
            btc_vol = qv
        elif base != "ETH":
            alt_vol += qv

    if btc_vol is None or total_vol <= 0:
        return None, None

    btc_d = (btc_vol / total_vol) * 100.0
    return btc_d, alt_vol


def _change_24h(history: list[tuple[float, float, float]], now: float, current: float, idx: int) -> float | None:
    target = now - 86400.0
    best: tuple[float, float, float] | None = None
    for ts, btc_d, total3 in history:  # chronological: stop at first entry past target
        if ts > target:
            break
        best = (ts, btc_d, total3)
    if best is None:
        return None
    past_val = best[idx]
    if not past_val:
        return None
    return (current - past_val) / past_val * 100.0


async def fetch_macro_data(exchange: Any = None, max_age: float = 1800.0, rest_gate: Any = None) -> MacroDataSnapshot:
    global _cached, _last_fetch, _history

    now = time.time()
    if _cached is not None and (now - _last_fetch) < max_age:
        return _cached

    if exchange is None:
        if _cached is not None:
            return replace(_cached, stale=True)
        return MacroDataSnapshot(timestamp=now, stale=True)

    btc_d, total3 = await _compute_ccxt_proxy(exchange, rest_gate)
    if btc_d is None:
        if _cached is not None:
            return replace(_cached, stale=True)
        return MacroDataSnapshot(timestamp=now, stale=True)

    _history.append((now, btc_d, total3 or 0.0))
    _history[:] = [(ts, b, t) for ts, b, t in _history if now - ts < _HISTORY_MAX_AGE]

    btc_d_chg = _change_24h(_history, now, btc_d, 1)
    total3_chg = _change_24h(_history, now, total3 or 0.0, 2)

    snapshot = MacroDataSnapshot(
        btc_d=btc_d,
        btc_d_change_24h=btc_d_chg,
        total3_cap=total3,
        total3_change_24h=total3_chg,
        timestamp=now,
    )
    _cached = snapshot
    _last_fetch = now
    return snapshot


def clear_macro_cache() -> None:
    global _cached, _last_fetch, _history
    _cached = None
    _last_fetch = 0.0
    _history = []

"""Minimum liquidity floors for hunt dynamic symbols (research batch 1)."""

from __future__ import annotations

from hunt_watch.targets import PINNED_SYMBOLS

MIN_QUOTE_VOL_24H_USD = 1_000_000.0
MIN_OPEN_INTEREST_USD = 100_000.0


def liquidity_skip_reason(
    *,
    quote_volume: float,
    oi: float | None,
    last_price: float,
    symbol: str = "",
) -> str | None:
    """Return error tag when symbol is too illiquid for reliable signals."""
    sym = symbol.upper()
    if sym in PINNED_SYMBOLS:
        return None
    if float(quote_volume or 0) < MIN_QUOTE_VOL_24H_USD:
        return f"liquidity_low_vol24h:{quote_volume:.0f}"
    if oi is not None and last_price > 0:
        oi_usd = float(oi) * last_price
        if oi_usd < MIN_OPEN_INTEREST_USD:
            return f"liquidity_low_oi:{oi_usd:.0f}"
    return None

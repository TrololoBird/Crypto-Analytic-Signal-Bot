"""Light universe screener → candidate pool for shortlist (target spec)."""

from __future__ import annotations

from typing import Any

from bot.domain.config import BotSettings


def light_scan_rows(
    ticker_rows: list[dict[str, Any]],
    settings: BotSettings,
) -> list[dict[str, Any]]:
    """Filter 24h tickers to liquid USDT perps before shortlist ranking."""
    uni = settings.universe
    min_vol = float(uni.min_quote_volume_usd)
    min_trades = int(uni.min_trade_count_24h)
    quote = uni.quote_asset.upper()

    out: list[dict[str, Any]] = []
    for row in ticker_rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol.endswith(quote):
            continue
        qv = float(row.get("quoteVolume") or row.get("quote_volume") or 0.0)
        trades = int(float(row.get("count") or row.get("trade_count") or 0))
        if qv < min_vol or trades < min_trades:
            continue
        out.append(row)
    return out

"""Historical kline window fetch for forensic replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import polars as pl

from bot.market.data import _timeframe_to_seconds

if TYPE_CHECKING:
    from bot.market.rest_impl import BinanceClientImpl


async def fetch_klines_window(
    client: BinanceClientImpl,
    symbol: str,
    interval: str,
    *,
    center: datetime,
    bars_before: int,
    bars_after: int,
) -> pl.DataFrame:
    """Fetch OHLCV window centered on an event timestamp."""
    seconds = _timeframe_to_seconds(interval)
    if seconds is None:
        msg = f"unsupported interval for forensic window: {interval}"
        raise ValueError(msg)
    if center.tzinfo is None:
        center = center.replace(tzinfo=UTC)
    else:
        center = center.astimezone(UTC)
    span = int(bars_before) + int(bars_after) + 5
    start = center - timedelta(seconds=seconds * max(1, bars_before))
    end = center + timedelta(seconds=seconds * max(1, bars_after))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    limit = min(1500, max(50, span))
    return await client.fetch_klines_between(
        symbol,
        interval,
        start_time_ms=start_ms,
        end_time_ms=end_ms,
        limit=limit,
    )


async def fetch_forensic_candle_pack(
    client: BinanceClientImpl,
    *,
    symbol: str,
    signal_timeframe: str,
    event_dt: datetime,
    bars_before: int = 60,
    bars_after: int = 60,
) -> dict[str, pl.DataFrame]:
    """Load signal TF + 1h + 4h + BTC 15m for one SL event."""
    packs: dict[str, pl.DataFrame] = {}
    for tf in (signal_timeframe, "1h", "4h"):
        if tf in packs:
            continue
        try:
            packs[tf] = await fetch_klines_window(
                client,
                symbol,
                tf,
                center=event_dt,
                bars_before=bars_before,
                bars_after=bars_after,
            )
        except Exception:
            packs[tf] = pl.DataFrame()
    if symbol != "BTCUSDT":
        try:
            packs["btc_15m"] = await fetch_klines_window(
                client,
                "BTCUSDT",
                "15m",
                center=event_dt,
                bars_before=bars_before,
                bars_after=bars_after,
            )
        except Exception:
            packs["btc_15m"] = pl.DataFrame()
    return packs


__all__ = ["fetch_forensic_candle_pack", "fetch_klines_window"]

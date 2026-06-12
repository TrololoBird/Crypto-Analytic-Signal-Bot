"""CCXT OHLCV → Polars frames (hunt kernel)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl

from engine.market._rest_frames import _drop_incomplete_ohlcv_tail

_TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

_KLINE_FRAME_SCHEMA = {
    "time": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "close_time": pl.Datetime("us", "UTC"),
    "quote_volume": pl.Float64,
    "num_trades": pl.Int64,
    "taker_buy_base_volume": pl.Float64,
    "taker_buy_quote_volume": pl.Float64,
    "open_time": pl.Datetime("us", "UTC"),
}


def _close_time_ms(open_ms: int, interval: str) -> int:
    step = _TIMEFRAME_SECONDS.get(interval, 60) * 1000
    return open_ms + step - 1


def ccxt_ohlcv_to_frame(rows: list[list[Any]], interval: str) -> pl.DataFrame:
    """Convert CCXT ``[ts, o, h, l, c, v]`` rows into hunt/bot kline schema."""
    if not rows:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    built: list[dict[str, Any]] = []
    for row in rows:
        if not row or len(row) < 6:
            continue
        try:
            open_ms = int(row[0])
            o, h, low, c, v = (
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            )
        except (TypeError, ValueError, IndexError):
            continue
        if open_ms <= 0 or c <= 0:
            continue
        close_ms = _close_time_ms(open_ms, interval)
        built.append(
            {
                "time": open_ms,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": v,
                "close_time": close_ms,
                "quote_volume": 0.0,
                "num_trades": 0,
                "taker_buy_base_volume": 0.0,
                "taker_buy_quote_volume": 0.0,
            }
        )
    if not built:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    frame = pl.DataFrame(built)
    return frame.with_columns(
        pl.from_epoch(pl.col("time"), time_unit="ms").dt.replace_time_zone("UTC").alias("time"),
        pl.from_epoch(pl.col("close_time"), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .alias("close_time"),
        pl.from_epoch(pl.col("time"), time_unit="ms").dt.replace_time_zone("UTC").alias("open_time"),
    )


def finalize_kline_frame(frame: pl.DataFrame, interval: str) -> pl.DataFrame:
    return _drop_incomplete_ohlcv_tail(frame, interval)


def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)

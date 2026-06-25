"""OHLCV frame builders and REST row coercion (extracted from rest_impl.py)."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from engine.market.data import _KLINE_COLUMNS, _KLINE_FRAME_SCHEMA, UTC

_LOG = logging.getLogger("bot.market.rest_frames")


def _timeframe_to_seconds(timeframe: str) -> int | None:
    mapping = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "6h": 21600,
        "8h": 28800,
        "12h": 43200,
        "1d": 86400,
    }
    return mapping.get(timeframe)


def _ohlcv_frame_has_incomplete_tail(df: pl.DataFrame, timeframe: str) -> bool:
    if df.is_empty():
        return False
    if "close_time" in df.columns:
        last_close = df["close_time"].tail(1).item()
        if isinstance(last_close, datetime):
            return datetime.now(UTC) <= last_close
    timeframe_seconds = _timeframe_to_seconds(timeframe)
    if timeframe_seconds is None:
        return False
    last_open = df["time"].tail(1).item()
    if not isinstance(last_open, datetime):
        return False
    return datetime.now(UTC) < last_open + timedelta(seconds=timeframe_seconds)


def _drop_incomplete_ohlcv_tail(df: pl.DataFrame, timeframe: str) -> pl.DataFrame:
    if df.is_empty():
        return df
    if "close_time" in df.columns:
        now = datetime.now(UTC)
        closed = df.filter(pl.col("close_time") < pl.lit(now))
        if closed.height != df.height:
            return closed
    if _ohlcv_frame_has_incomplete_tail(df, timeframe):
        return df.head(df.height - 1)
    return df


def _klines_to_frame(rows: Any) -> pl.DataFrame:
    """Convert raw Binance kline rows into a Polars DataFrame using vectorized operations.

    Expected REST input is a list of lists with at least 11 items. The function
    also accepts dict rows from WebSocket/backfill paths so callers can share
    one conversion boundary without silently returning an empty frame.
    """
    if not rows:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    columns = list(_KLINE_COLUMNS)
    valid_rows: list[list[Any]] = []
    dict_rows: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, list) and len(row) >= 11:
            valid_rows.append(row[:11])
            continue
        if isinstance(row, Mapping):
            dict_rows.append({column: row.get(column) for column in columns})
    if not valid_rows and (not dict_rows):
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)
    frames: list[pl.DataFrame] = []
    if valid_rows:
        frames.append(pl.DataFrame(valid_rows, schema=columns, orient="row"))
    if dict_rows:
        frames.append(pl.DataFrame(dict_rows))
    frame = frames[0] if len(frames) == 1 else pl.concat(frames, how="diagonal")
    time_exprs: list[pl.Expr] = []
    for column in ("time", "close_time"):
        dtype = frame.schema.get(column)
        if dtype is not None and getattr(dtype, "is_temporal", lambda: False)():
            time_exprs.append(pl.col(column))
        elif dtype == pl.String:
            time_exprs.append(pl.col(column).str.to_datetime(strict=False, time_zone="UTC"))
        else:
            time_exprs.append(
                pl.from_epoch(pl.col(column).cast(pl.Int64), time_unit="ms").dt.replace_time_zone(
                    "UTC"
                )
            )
    return frame.with_columns(
        [
            time_exprs[0].alias("time"),
            time_exprs[1].alias("close_time"),
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("volume").cast(pl.Float64),
            pl.col("quote_volume").cast(pl.Float64),
            pl.col("num_trades").cast(pl.Int64),
            pl.col("taker_buy_base_volume").cast(pl.Float64),
            pl.col("taker_buy_quote_volume").cast(pl.Float64),
            pl.from_epoch(pl.col("time"), time_unit="ms")
            .dt.replace_time_zone("UTC")
            .alias("open_time"),
        ]
    )


def validate_kline_frame(
    frame: pl.DataFrame,
    timeframe: str,
    *,
    symbol: str = "",
) -> pl.DataFrame:
    """Remove duplicate close_times and log timestamp gaps (п.35).

    Called after ``_klines_to_frame`` in REST kline fetch methods.
    Returns the cleaned frame (may be shorter than input).
    """
    if frame.is_empty() or "close_time" not in frame.columns:
        return frame
    before = frame.height
    frame = frame.unique(subset=["close_time"], keep="last", maintain_order=True)
    dupes = before - frame.height
    if dupes > 0:
        _LOG.warning(
            "kline duplicates removed | symbol=%s tf=%s dupes=%d", symbol, timeframe, dupes
        )
    interval_s = _timeframe_to_seconds(timeframe)
    if interval_s and frame.height >= 2:
        times_ms = frame["close_time"].cast(pl.Int64) // 1_000_000
        diffs = times_ms.diff().drop_nulls()
        expected_ms = interval_s * 1000
        gaps = int((diffs > expected_ms * 1.5).sum())
        if gaps > 0:
            _LOG.warning(
                "kline gaps detected | symbol=%s tf=%s gaps=%d interval=%ds",
                symbol,
                timeframe,
                gaps,
                interval_s,
            )
    return frame


def _unwrap_model(value: Any) -> Any:
    if hasattr(value, "actual_instance") and value.actual_instance is not None:
        return value.actual_instance
    return value


def _coerce_rest_row(item: Any) -> Mapping[str, Any]:
    row = _unwrap_model(item)
    if isinstance(row, Mapping):
        return row
    if hasattr(row, "model_dump"):
        dumped = row.model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    msg = f"Unsupported REST row payload type: {type(item)!r}"
    raise TypeError(msg)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _parse_depth_levels(raw_levels: Any, *, reverse: bool) -> tuple[tuple[float, float], ...]:
    parsed: list[tuple[float, float]] = []
    if not isinstance(raw_levels, list):
        return ()
    for raw in raw_levels:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        try:
            price = float(raw[0])
            qty = float(raw[1])
        except (TypeError, ValueError):
            continue
        if price <= 0.0 or qty <= 0.0 or (not math.isfinite(price)) or (not math.isfinite(qty)):
            continue
        parsed.append((price, qty))
    parsed.sort(key=lambda item: item[0], reverse=reverse)
    return tuple(parsed)

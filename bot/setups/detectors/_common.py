"""Shared spec detector primitives."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import polars as pl

from ...domain.config import BotSettings
from ...domain.schemas import PreparedSymbol, Signal
from ...features import _swing_points
from ...features.shared import wilder_mean
from ...domain.catalog_guards import catalog_allows_signal
from ...domain.strategy_catalog import catalog_default_params
from .. import _build_signal, _compute_dynamic_score, _reject

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpecHit:
    strategy: str
    direction: str
    entry: float
    stop_basis: float
    atr: float
    timeframe: str
    reasons: tuple[str, ...]
    structure_clarity: float = 0.6
    vol_ratio: float = 1.0
    rsi: float = 50.0
    source_index: int | None = None


@dataclass(frozen=True, slots=True)
class SpecHit:
    strategy: str
    direction: str
    entry: float
    stop_basis: float
    atr: float
    timeframe: str
    reasons: tuple[str, ...]
    structure_clarity: float = 0.6
    vol_ratio: float = 1.0
    rsi: float = 50.0
    source_index: int | None = None


def as_float(value: object, default: float = 0.0) -> float:
    try:
        numeric = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def required_columns(frame: pl.DataFrame, columns: tuple[str, ...]) -> list[str]:
    return [column for column in columns if column not in frame.columns]


def _feature_or_expr(
    frame: pl.DataFrame,
    source_column: str,
    fallback: pl.Expr,
    alias: str,
) -> pl.Expr:
    if source_column in frame.columns:
        return pl.col(source_column).cast(pl.Float64, strict=False).alias(alias)
    return fallback.alias(alias)


def _feature_pair_or_expr(
    frame: pl.DataFrame,
    upper_column: str,
    lower_column: str,
    upper_fallback: pl.Expr,
    lower_fallback: pl.Expr,
    upper_alias: str,
    lower_alias: str,
) -> tuple[pl.Expr, pl.Expr]:
    if upper_column in frame.columns and lower_column in frame.columns:
        return (
            pl.col(upper_column).cast(pl.Float64, strict=False).alias(upper_alias),
            pl.col(lower_column).cast(pl.Float64, strict=False).alias(lower_alias),
        )
    return upper_fallback.alias(upper_alias), lower_fallback.alias(lower_alias)


def with_spec_columns(frame: pl.DataFrame) -> pl.DataFrame:
    """Add strict spec columns while reusing prepared feature columns when present."""
    if frame.is_empty():
        return frame
    required = required_columns(frame, ("open", "high", "low", "close", "volume"))
    if required:
        return frame
    if "_spec_idx" in frame.columns:
        return frame

    work = frame.with_row_index("_spec_idx")
    prev_close = pl.col("close").shift(1)
    tr_expr = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - prev_close).abs(),
        (pl.col("low") - prev_close).abs(),
    )
    if {"taker_buy_base_volume", "volume"}.issubset(set(work.columns)):
        spec_delta = 2.0 * pl.col("taker_buy_base_volume") - pl.col("volume")
    elif "delta_ratio" in work.columns:
        spec_delta = (pl.col("delta_ratio") - 0.5) * 2.0 * pl.col("volume")
    else:
        spec_delta = pl.lit(None).cast(pl.Float64)

    if "rsi14" in work.columns:
        rsi_expr: pl.Expr | pl.Series = pl.col("rsi14").cast(pl.Float64, strict=False).alias("rsi14")
    else:
        close = work["close"].cast(pl.Float64, strict=False)
        delta = close.diff()
        gain = delta.clip(lower_bound=0.0)
        loss = (-delta).clip(lower_bound=0.0)
        avg_gain = wilder_mean(gain, period=14, name="spec_avg_gain", seed_offset=1)
        avg_loss = wilder_mean(loss, period=14, name="spec_avg_loss", seed_offset=1)
        raw_rsi = (100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))).fill_nan(50.0)
        rsi_values: list[float] = []
        for gain_value, loss_value, raw_value in zip(
            avg_gain.to_list(),
            avg_loss.to_list(),
            raw_rsi.to_list(),
            strict=False,
        ):
            gain_f = as_float(gain_value, 0.0)
            loss_f = as_float(loss_value, 0.0)
            if loss_f == 0.0 and gain_f > 0.0:
                rsi_values.append(100.0)
            elif gain_f == 0.0 and loss_f > 0.0:
                rsi_values.append(0.0)
            elif gain_f == 0.0 and loss_f == 0.0:
                rsi_values.append(50.0)
            else:
                rsi_values.append(as_float(raw_value, 50.0))
        rsi_expr = pl.Series("rsi14", rsi_values, dtype=pl.Float64)

    if "vwap" in work.columns:
        vwap_expr = pl.col("vwap").cast(pl.Float64, strict=False).alias("vwap")
    else:
        time_column = next(
            (column for column in ("open_time", "time", "close_time") if column in work.columns),
            None,
        )
        typical_price = (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0
        if time_column is not None and getattr(
            work.schema.get(time_column), "is_temporal", lambda: False
        )():
            vwap_expr = (
                (typical_price * pl.col("volume")).cum_sum().over(pl.col(time_column).dt.date())
                / pl.col("volume").cum_sum().over(pl.col(time_column).dt.date())
            ).alias("vwap")
        else:
            vwap_expr = (
                (typical_price * pl.col("volume")).cum_sum() / pl.col("volume").cum_sum()
            ).alias("vwap")

    pass1: list[pl.Expr | pl.Series] = [
        tr_expr.alias("spec_tr"),
        _feature_or_expr(work, "atr14", tr_expr.rolling_mean(14), "spec_atr14"),
        _feature_or_expr(work, "atr20", tr_expr.rolling_mean(20), "spec_atr20"),
        _feature_or_expr(
            work,
            "volume_mean20",
            pl.col("volume").rolling_mean(20),
            "spec_volume_mean20",
        ),
        _feature_or_expr(
            work,
            "ema20",
            pl.col("close").ewm_mean(span=20, adjust=False),
            "spec_ema20",
        ),
        _feature_or_expr(
            work,
            "ema21",
            pl.col("close").ewm_mean(span=21, adjust=False),
            "spec_ema21",
        ),
        _feature_or_expr(
            work,
            "ema50",
            pl.col("close").ewm_mean(span=50, adjust=False),
            "spec_ema50",
        ),
        _feature_or_expr(
            work,
            "ema200",
            pl.col("close").ewm_mean(span=200, adjust=False),
            "spec_ema200",
        ),
        _feature_or_expr(work, "sma20", pl.col("close").rolling_mean(20), "spec_sma20"),
        (pl.col("high").shift(1).rolling_max(20)).alias("spec_prev_high20"),
        (pl.col("low").shift(1).rolling_min(20)).alias("spec_prev_low20"),
        (pl.col("high").shift(1).rolling_max(30)).alias("spec_prev_high30"),
        (pl.col("low").shift(1).rolling_min(30)).alias("spec_prev_low30"),
        (pl.col("high") - pl.col("low")).alias("spec_range"),
        (pl.col("close") - pl.col("open")).abs().alias("spec_body"),
        (pl.col("high") - pl.max_horizontal(pl.col("open"), pl.col("close"))).alias(
            "spec_upper_wick"
        ),
        (pl.min_horizontal(pl.col("open"), pl.col("close")) - pl.col("low")).alias(
            "spec_lower_wick"
        ),
        spec_delta.alias("spec_delta"),
        rsi_expr,
        vwap_expr,
    ]
    work = work.with_columns(pass1)

    bb_std = pl.col("close").rolling_std(window_size=20, ddof=1)
    if "kc_upper_15" in work.columns and "kc_lower_15" in work.columns:
        kc15_upper_value = pl.col("kc_upper_15").cast(pl.Float64, strict=False)
        kc15_lower_value = pl.col("kc_lower_15").cast(pl.Float64, strict=False)
    else:
        kc15_upper_value = pl.col("spec_ema20") + 1.5 * pl.col("spec_atr20")
        kc15_lower_value = pl.col("spec_ema20") - 1.5 * pl.col("spec_atr20")
    if "kc_upper" in work.columns and "kc_lower" in work.columns:
        kc20_upper_value = pl.col("kc_upper").cast(pl.Float64, strict=False)
        kc20_lower_value = pl.col("kc_lower").cast(pl.Float64, strict=False)
    else:
        kc20_upper_value = pl.col("spec_ema20") + 2.0 * pl.col("spec_atr14")
        kc20_lower_value = pl.col("spec_ema20") - 2.0 * pl.col("spec_atr14")
    if "bb_upper" in work.columns and "bb_lower" in work.columns:
        bb_upper_value = pl.col("bb_upper").cast(pl.Float64, strict=False)
        bb_lower_value = pl.col("bb_lower").cast(pl.Float64, strict=False)
    else:
        bb_upper_value = pl.col("spec_sma20") + 2.0 * bb_std
        bb_lower_value = pl.col("spec_sma20") - 2.0 * bb_std

    work = work.with_columns(
        [
            bb_upper_value.alias("spec_bb_upper"),
            bb_lower_value.alias("spec_bb_lower"),
            kc15_upper_value.alias("spec_kc15_upper"),
            kc15_lower_value.alias("spec_kc15_lower"),
            kc20_upper_value.alias("spec_kc20_upper"),
            kc20_lower_value.alias("spec_kc20_lower"),
            ((bb_upper_value < kc15_upper_value) & (bb_lower_value > kc15_lower_value)).alias(
                "spec_squeeze"
            ),
            (
                pl.col("spec_body")
                / pl.when(pl.col("spec_range") > 0.0).then(pl.col("spec_range")).otherwise(1e-8)
            ).alias("spec_body_ratio"),
            (
                pl.col("spec_upper_wick")
                / pl.when(pl.col("spec_body") > 0.0).then(pl.col("spec_body")).otherwise(1e-8)
            ).alias("spec_upper_wick_ratio"),
            (
                pl.col("spec_lower_wick")
                / pl.when(pl.col("spec_body") > 0.0).then(pl.col("spec_body")).otherwise(1e-8)
            ).alias("spec_lower_wick_ratio"),
            pl.col("spec_delta").abs().rolling_mean(20).alias("spec_abs_delta_mean20"),
            pl.col("spec_delta").rolling_std(20).alias("spec_delta_std20"),
            pl.col("spec_delta").fill_null(0.0).cum_sum().alias("spec_cvd"),
        ]
    )
    return work


def build_spec_signal(
    *,
    prepared: PreparedSymbol,
    settings: BotSettings,
    setup_id: str,
    family: str,
    hit: SpecHit,
    defaults: dict[str, float],
    params: dict[str, float] | None = None,
) -> Signal | None:
    effective = {**catalog_default_params(setup_id), **defaults, **(params or {})}
    if not catalog_allows_signal(
        prepared,
        setup_id=setup_id,
        direction=hit.direction,
        family=family,
        confirmation_profile=str(effective.get("confirmation_profile", "trend_follow")),
        params=effective,
    ):
        return None
    min_rr = float(effective.get("min_rr", 1.9))
    sl_buffer = float(effective.get("sl_buffer_atr", 0.55))
    entry = float(hit.entry)
    atr = float(hit.atr)
    if entry <= 0.0 or atr <= 0.0:
        _reject(prepared, setup_id, "invalid_indicator_state", entry=entry, atr=atr)
        return None
    if hit.direction == "long":
        stop = min(float(hit.stop_basis), entry) - atr * sl_buffer
        risk = entry - stop
        tp1 = entry + risk * min_rr
        tp2 = entry + risk * max(min_rr + 0.4, 2.0)
    else:
        stop = max(float(hit.stop_basis), entry) + atr * sl_buffer
        risk = stop - entry
        tp1 = entry - risk * min_rr
        tp2 = entry - risk * max(min_rr + 0.4, 2.0)
    if risk <= 0.0:
        _reject(prepared, setup_id, "invalid_stop", stop=stop, entry=entry)
        return None
    score = _compute_dynamic_score(
        direction=hit.direction,
        base_score=float(effective.get("base_score", 0.52)),
        vol_ratio=max(0.1, float(hit.vol_ratio)),
        rsi=float(hit.rsi),
        structure_clarity=max(0.0, min(1.0, float(hit.structure_clarity))),
    )
    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=hit.direction,
        score=score,
        timeframe=hit.timeframe,
        reasons=[*hit.reasons, "spec_contract=2026-05", f"limit_entry={entry:.4f}"],
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=entry,
        atr=atr,
    )


def _latest_values(work: pl.DataFrame) -> dict[str, float]:
    if work.is_empty():
        return {}
    last = work.row(-1, named=True)
    return {key: as_float(value) for key, value in last.items() if isinstance(key, str)}


def _pivot_rows(
    work: pl.DataFrame,
    *,
    price_column: str,
    indicator_column: str,
    pivot: str,
    max_lookback: int = 50,
) -> list[dict[str, float]]:
    if work.height < 7 or price_column not in work.columns or indicator_column not in work.columns:
        return []
    current_idx = int(work.item(-1, "_spec_idx"))
    high_mask, low_mask = _swing_points(work, n=2, include_unconfirmed_tail=False)
    mask = low_mask if pivot == "low" else high_mask
    # Live-safe divergence pivots: exclude the two tail bars before comparing neighbors.
    mask = mask & (work["_spec_idx"] <= current_idx - 2)
    confirmed = work.filter(mask).to_dicts()
    return [
        {
            "idx": float(row["_spec_idx"]),
            "price": as_float(row.get(price_column)),
            "indicator": as_float(row.get(indicator_column)),
        }
        for row in confirmed[-8:]
        if current_idx - int(row["_spec_idx"]) <= max_lookback
    ]


def _row_volume_ratio(row: dict[str, object]) -> float:
    volume_ratio = as_float(row.get("volume_ratio20"), 0.0)
    if volume_ratio > 0.0:
        return volume_ratio
    volume_mean = as_float(row.get("spec_volume_mean20"))
    volume = as_float(row.get("volume"))
    return volume / volume_mean if volume_mean > 0.0 else 1.0


def _clean_impulse(work: pl.DataFrame, start_idx: int, end_idx: int, direction: str) -> bool:
    if end_idx - start_idx < 3:
        return False
    rows = work.slice(start_idx, end_idx - start_idx + 1).to_dicts()
    for prev, row in zip(rows, rows[1:], strict=False):
        close = as_float(row.get("close"))
        prev_open = as_float(prev.get("open"))
        if direction == "long" and close < prev_open:
            return False
        if direction == "short" and close > prev_open:
            return False
    return True


def _valid_order_block_rows(work: pl.DataFrame, max_age: int = 80) -> list[dict[str, object]]:
    rows = work.to_dicts()
    current_idx = int(work.item(-1, "_spec_idx"))
    zones: list[dict[str, object]] = []
    impulse_lookback = 5
    impulse_atr_mult = 1.5
    for pos, row in enumerate(rows):
        idx = int(row["_spec_idx"])
        if current_idx - idx > max_age:
            continue
        confirm_end = min(pos + impulse_lookback, len(rows) - 1)
        if confirm_end <= pos or current_idx < int(rows[confirm_end]["_spec_idx"]):
            continue
        confirmation_rows = rows[pos + 1 : confirm_end + 1]
        if len(confirmation_rows) < 3:
            continue
        open_ = as_float(row.get("open"))
        close = as_float(row.get("close"))
        low = as_float(row.get("low"))
        high = as_float(row.get("high"))
        atr = as_float(row.get("spec_atr14"))
        prev_high = as_float(row.get("spec_prev_high20"))
        prev_low = as_float(row.get("spec_prev_low20"))
        volume = as_float(row.get("volume"))
        volume_mean = as_float(row.get("spec_volume_mean20"))
        if min(open_, close, low, high, atr, volume_mean) <= 0.0:
            continue
        final_close = as_float(confirmation_rows[-1].get("close"))
        bull_impulse = close < open_ and final_close - close > impulse_atr_mult * atr
        bear_impulse = close > open_ and close - final_close > impulse_atr_mult * atr
        if bull_impulse and (prev_high <= 0.0 or final_close > prev_high):
            zones.append(
                {
                    **row,
                    "direction": "long",
                    "bottom": low,
                    "top": high,
                    "age": current_idx - idx,
                    "volume_ok": volume >= volume_mean,
                    "impulse_close": final_close,
                    "impulse_pos": confirm_end,
                }
            )
        if bear_impulse and (prev_low <= 0.0 or final_close < prev_low):
            zones.append(
                {
                    **row,
                    "direction": "short",
                    "bottom": low,
                    "top": high,
                    "age": current_idx - idx,
                    "volume_ok": volume >= volume_mean,
                    "impulse_close": final_close,
                    "impulse_pos": confirm_end,
                }
            )
    return zones


def current_utc_hour(frame: pl.DataFrame) -> int:
    if frame.is_empty():
        return datetime.now(timezone.utc).hour
    for column in ("open_time", "time", "close_time"):
        if column not in frame.columns:
            continue
        value = frame.item(-1, column)
        if isinstance(value, datetime):
            dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return dt.hour
    return datetime.now(timezone.utc).hour

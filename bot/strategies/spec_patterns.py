"""Canonical strategy-spec pattern masks.

These helpers implement the contract from ``Trading Bot - 38 Strategy
Specifications``.  They are intentionally public-data and dataframe-only: no
REST calls, no private endpoints, no account state.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
import polars as pl

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..features import _swing_points
from ..features_shared import wilder_mean
from ..setups import _build_signal, _compute_dynamic_score, _reject

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
    effective = {**defaults, **(params or {})}
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


def detect_fvg(frame: pl.DataFrame, *, timeframe: str = "15m", max_age: int = 20) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 5:
        return None
    work = work.with_columns(
        [
            pl.col("high").shift(2).alias("spec_h2"),
            pl.col("low").shift(2).alias("spec_l2"),
            (pl.col("low") > pl.col("high").shift(2)).alias("spec_bull_fvg"),
            (pl.col("high") < pl.col("low").shift(2)).alias("spec_bear_fvg"),
        ]
    )
    current_close = as_float(work.item(-1, "close"))
    current_idx = int(work.item(-1, "_spec_idx"))
    candidates = work.filter(pl.col("spec_bull_fvg") | pl.col("spec_bear_fvg")).tail(max_age + 1)
    for row in reversed(candidates.to_dicts()):
        idx = int(row["_spec_idx"])
        age = current_idx - idx
        if age > max_age:
            continue
        atr = as_float(row.get("spec_atr14"), as_float(work.item(-1, "spec_atr14")))
        vol_ratio = as_float(row.get("volume_ratio20"), 1.0)
        rsi = as_float(work.item(-1, "rsi14"), 50.0)
        if bool(row.get("spec_bull_fvg")):
            bottom = as_float(row.get("spec_h2"))
            top = as_float(row.get("low"))
            if bottom <= current_close <= top:
                return SpecHit(
                    strategy="fvg_setup",
                    direction="long",
                    entry=(bottom + top) / 2.0,
                    stop_basis=bottom,
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(f"bull_fvg zone={bottom:.4f}-{top:.4f}", f"age={age}"),
                    vol_ratio=vol_ratio,
                    rsi=rsi,
                    source_index=idx,
                )
        if bool(row.get("spec_bear_fvg")):
            bottom = as_float(row.get("high"))
            top = as_float(row.get("spec_l2"))
            if bottom <= current_close <= top:
                return SpecHit(
                    strategy="fvg_setup",
                    direction="short",
                    entry=(bottom + top) / 2.0,
                    stop_basis=top,
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(f"bear_fvg zone={bottom:.4f}-{top:.4f}", f"age={age}"),
                    vol_ratio=vol_ratio,
                    rsi=rsi,
                    source_index=idx,
                )
    return None


def _row_volume_ratio(row: dict[str, object]) -> float:
    volume_ratio = as_float(row.get("volume_ratio20"), 0.0)
    if volume_ratio > 0.0:
        return volume_ratio
    volume_mean = as_float(row.get("spec_volume_mean20"))
    volume = as_float(row.get("volume"))
    return volume / volume_mean if volume_mean > 0.0 else 1.0


def detect_bos_choch(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    max_age: int = 20,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    current = _latest_values(work)
    current_idx = int(work.item(-1, "_spec_idx"))
    current_close = current.get("close", 0.0)
    current_atr = current.get("spec_atr14", 0.0)
    if current_close <= 0.0 or current_atr <= 0.0:
        return None
    candidates = work.tail(max_age + 1).to_dicts()
    for row in reversed(candidates):
        idx = int(row["_spec_idx"])
        age = current_idx - idx
        if age > max_age:
            continue
        close = as_float(row.get("close"))
        prev_high = as_float(row.get("spec_prev_high20"))
        prev_low = as_float(row.get("spec_prev_low20"))
        atr = as_float(row.get("spec_atr14"), current_atr)
        if min(close, prev_high, prev_low, atr) <= 0.0:
            continue
        if close > prev_high and current_close > prev_high:
            return SpecHit(
                strategy="bos_choch",
                direction="long",
                entry=current_close if age == 0 else prev_high,
                stop_basis=prev_high - atr,
                atr=atr,
                timeframe=timeframe,
                reasons=(f"body_break_above_swing={prev_high:.4f}", f"break_age={age}"),
                structure_clarity=min(1.0, (close - prev_high) / max(atr, 1e-8)),
                vol_ratio=_row_volume_ratio(row),
                rsi=as_float(row.get("rsi14"), 50.0),
                source_index=idx,
            )
        if close < prev_low and current_close < prev_low:
            return SpecHit(
                strategy="bos_choch",
                direction="short",
                entry=current_close if age == 0 else prev_low,
                stop_basis=prev_low + atr,
                atr=atr,
                timeframe=timeframe,
                reasons=(f"body_break_below_swing={prev_low:.4f}", f"break_age={age}"),
                structure_clarity=min(1.0, (prev_low - close) / max(atr, 1e-8)),
                vol_ratio=_row_volume_ratio(row),
                rsi=as_float(row.get("rsi14"), 50.0),
                source_index=idx,
            )
    return None


def detect_structure_break_retest(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    lookback: int = 20,
    tolerance_pct: float = 0.001,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    current = _latest_values(work)
    atr = current.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    close = current["close"]
    low = current["low"]
    high = current["high"]
    current_idx = int(work.item(-1, "_spec_idx"))
    candidates = work.tail(lookback + 2).head(lookback + 1).to_dicts()
    for row in reversed(candidates):
        idx = int(row["_spec_idx"])
        age = current_idx - idx
        if age < 1 or age > lookback:
            continue
        break_close = as_float(row.get("close"))
        prev_high = as_float(row.get("spec_prev_high20"))
        prev_low = as_float(row.get("spec_prev_low20"))
        if min(break_close, prev_high, prev_low) <= 0.0:
            continue
        if (
            break_close > prev_high
            and low <= prev_high * (1.0 + tolerance_pct)
            and close > prev_high
        ):
            return SpecHit(
                strategy="structure_break_retest",
                direction="long",
                entry=prev_high,
                stop_basis=min(low, prev_high - atr * 0.25),
                atr=atr,
                timeframe=timeframe,
                reasons=(f"bos_retest_level={prev_high:.4f}", f"break_age={age}"),
                structure_clarity=0.72,
                vol_ratio=current.get("volume_ratio20", 1.0),
                rsi=current.get("rsi14", 50.0),
                source_index=idx,
            )
        if (
            break_close < prev_low
            and high >= prev_low * (1.0 - tolerance_pct)
            and close < prev_low
        ):
            return SpecHit(
                strategy="structure_break_retest",
                direction="short",
                entry=prev_low,
                stop_basis=max(high, prev_low + atr * 0.25),
                atr=atr,
                timeframe=timeframe,
                reasons=(f"bos_retest_level={prev_low:.4f}", f"break_age={age}"),
                structure_clarity=0.72,
                vol_ratio=current.get("volume_ratio20", 1.0),
                rsi=current.get("rsi14", 50.0),
                source_index=idx,
            )
    return None


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


def detect_structure_pullback(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 35:
        return None
    current = _latest_values(work)
    atr = current.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    window = work.tail(30)
    rows = window.to_dicts()
    lows = [(i, as_float(row.get("low"))) for i, row in enumerate(rows)]
    highs = [(i, as_float(row.get("high"))) for i, row in enumerate(rows)]
    low_pos, swing_low = min(lows, key=lambda item: item[1])
    high_pos, swing_high = max(highs, key=lambda item: item[1])
    close = current["close"]
    if swing_high <= swing_low:
        return None
    if close > current.get("spec_ema50", close) and low_pos < high_pos:
        if not _clean_impulse(window, low_pos, high_pos, "long"):
            return None
        fib50 = swing_low + 0.5 * (swing_high - swing_low)
        fib618 = swing_low + 0.618 * (swing_high - swing_low)
        if fib50 <= close <= fib618:
            return SpecHit(
                strategy="structure_pullback",
                direction="long",
                entry=close,
                stop_basis=swing_low,
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ote_zone={fib50:.4f}-{fib618:.4f}", "clean_bull_impulse"),
                structure_clarity=0.70,
                vol_ratio=current.get("volume_ratio20", 1.0),
                rsi=current.get("rsi14", 50.0),
            )
    if close < current.get("spec_ema50", close) and high_pos < low_pos:
        if not _clean_impulse(window, high_pos, low_pos, "short"):
            return None
        fib50 = swing_high - 0.5 * (swing_high - swing_low)
        fib618 = swing_high - 0.618 * (swing_high - swing_low)
        zone_low = min(fib50, fib618)
        zone_high = max(fib50, fib618)
        if zone_low <= close <= zone_high:
            return SpecHit(
                strategy="structure_pullback",
                direction="short",
                entry=close,
                stop_basis=swing_high,
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ote_zone={zone_low:.4f}-{zone_high:.4f}", "clean_bear_impulse"),
                structure_clarity=0.70,
                vol_ratio=current.get("volume_ratio20", 1.0),
                rsi=current.get("rsi14", 50.0),
            )
    return None


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


def detect_order_block(frame: pl.DataFrame, *, timeframe: str = "1h") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 30:
        return None
    current = _latest_values(work)
    atr = current.get("spec_atr14", 0.0)
    close = current.get("close", 0.0)
    if atr <= 0.0 or close <= 0.0:
        return None
    for zone in reversed(_valid_order_block_rows(work)):
        bottom = as_float(zone.get("bottom"))
        top = as_float(zone.get("top"))
        if bottom <= close <= top:
            direction = str(zone["direction"])
            return SpecHit(
                strategy="order_block",
                direction=direction,
                entry=(bottom + top) / 2.0,
                stop_basis=bottom if direction == "long" else top,
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ob_zone={bottom:.4f}-{top:.4f}", f"age={int(zone['age'])}"),
                structure_clarity=0.76,
                vol_ratio=current.get("volume_ratio20", 1.0),
                rsi=current.get("rsi14", 50.0),
                source_index=int(zone["_spec_idx"]),
            )
    return None


def detect_breaker_block(frame: pl.DataFrame, *, timeframe: str = "1h") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 35:
        return None
    current = _latest_values(work)
    atr = current.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    rows = work.to_dicts()
    current_low = current["low"]
    current_high = current["high"]
    current_close = current["close"]
    for zone in reversed(_valid_order_block_rows(work, max_age=120)):
        if not bool(zone.get("volume_ok")):
            continue
        direction = str(zone["direction"])
        bottom = as_float(zone.get("bottom"))
        top = as_float(zone.get("top"))
        source_idx = int(zone["_spec_idx"])
        break_rows = [row for row in rows if int(row["_spec_idx"]) > source_idx]
        if direction == "long":
            broken = any(as_float(row.get("close")) < bottom for row in break_rows)
            retested = current_high >= bottom and current_close < top
            if broken and retested:
                return SpecHit(
                    strategy="breaker_block",
                    direction="short",
                    entry=(bottom + top) / 2.0,
                    stop_basis=top,
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(f"bull_ob_flipped_resistance={bottom:.4f}-{top:.4f}",),
                    structure_clarity=0.74,
                    vol_ratio=current.get("volume_ratio20", 1.0),
                    rsi=current.get("rsi14", 50.0),
                    source_index=source_idx,
                )
        else:
            broken = any(as_float(row.get("close")) > top for row in break_rows)
            retested = current_low <= top and current_close > bottom
            if broken and retested:
                return SpecHit(
                    strategy="breaker_block",
                    direction="long",
                    entry=(bottom + top) / 2.0,
                    stop_basis=bottom,
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(f"bear_ob_flipped_support={bottom:.4f}-{top:.4f}",),
                    structure_clarity=0.74,
                    vol_ratio=current.get("volume_ratio20", 1.0),
                    rsi=current.get("rsi14", 50.0),
                    source_index=source_idx,
                )
    return None


def detect_liquidity_sweep(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    prev_high = row.get("spec_prev_high20", 0.0)
    prev_low = row.get("spec_prev_low20", 0.0)
    close = row.get("close", 0.0)
    high = row.get("high", 0.0)
    low = row.get("low", 0.0)
    vol_ratio = row.get("volume_ratio20", 1.0)
    rsi = row.get("rsi14", 50.0)
    if atr <= 0.0:
        return None
    if high > prev_high and close < prev_high and (high - close) > atr * 0.3:
        return SpecHit(
            strategy="liquidity_sweep",
            direction="short",
            entry=prev_high,
            stop_basis=high,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"sweep_high level={prev_high:.4f}", f"wick_atr={(high-close)/atr:.2f}"),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    if low < prev_low and close > prev_low and (close - low) > atr * 0.3:
        return SpecHit(
            strategy="liquidity_sweep",
            direction="long",
            entry=prev_low,
            stop_basis=low,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"sweep_low level={prev_low:.4f}", f"wick_atr={(close-low)/atr:.2f}"),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    return None


def detect_turtle_soup(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    high = row["high"]
    low = row["low"]
    close = row["close"]
    upper = row.get("spec_prev_high20", 0.0)
    lower = row.get("spec_prev_low20", 0.0)
    vol_ratio = row.get("volume_ratio20", 1.0)
    rsi = row.get("rsi14", 50.0)
    if low < lower and close > lower:
        return SpecHit(
            strategy="turtle_soup",
            direction="long",
            entry=lower,
            stop_basis=low,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"donchian_false_break_low={lower:.4f}",),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    if high > upper and close < upper:
        return SpecHit(
            strategy="turtle_soup",
            direction="short",
            entry=upper,
            stop_basis=high,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"donchian_false_break_high={upper:.4f}",),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    return None


def detect_stop_hunt(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 20:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    recent = work.tail(10)
    high = row["high"]
    low = row["low"]
    close = row["close"]
    prev_high = row.get("spec_prev_high20", 0.0)
    prev_low = row.get("spec_prev_low20", 0.0)
    upper_wick = row.get("spec_upper_wick", 0.0)
    lower_wick = row.get("spec_lower_wick", 0.0)
    upper_ratio = row.get("spec_upper_wick_ratio", 0.0)
    lower_ratio = row.get("spec_lower_wick_ratio", 0.0)
    high_touches = recent.filter((pl.col("high") - prev_high).abs() <= atr * 0.25).height
    low_touches = recent.filter((pl.col("low") - prev_low).abs() <= atr * 0.25).height
    if (
        high > prev_high
        and close < prev_high
        and upper_ratio > 2.0
        and upper_wick > atr * 0.5
        and high_touches >= 2
    ):
        return SpecHit(
            strategy="stop_hunt_detection",
            direction="short",
            entry=prev_high,
            stop_basis=high,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"stop_cluster_high={prev_high:.4f}", f"touches={high_touches}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    if (
        low < prev_low
        and close > prev_low
        and lower_ratio > 2.0
        and lower_wick > atr * 0.5
        and low_touches >= 2
    ):
        return SpecHit(
            strategy="stop_hunt_detection",
            direction="long",
            entry=prev_low,
            stop_basis=low,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"stop_cluster_low={prev_low:.4f}", f"touches={low_touches}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    return None


def detect_wyckoff_spring(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 35:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    support = row.get("spec_prev_low30", 0.0)
    volume = row.get("volume", 0.0)
    volume_mean = row.get("spec_volume_mean20", 0.0)
    if (
        atr > 0.0
        and support > 0.0
        and row["low"] < support
        and row["close"] > support
        and volume < volume_mean * 0.8
    ):
        return SpecHit(
            strategy="wyckoff_spring",
            direction="long",
            entry=support,
            stop_basis=row["low"],
            atr=atr,
            timeframe=timeframe,
            reasons=(f"spring_support={support:.4f}", f"volume_vs_mean={volume/volume_mean:.2f}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    return None


def detect_wick_trap(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    body = row.get("spec_body", 0.0)
    if atr <= 0.0 or body < atr * 0.3:
        return None
    prev_high = row.get("spec_prev_high20", 0.0)
    prev_low = row.get("spec_prev_low20", 0.0)
    if row["low"] < prev_low and row.get("spec_lower_wick_ratio", 0.0) > 2.0:
        return SpecHit(
            strategy="wick_trap_reversal",
            direction="long",
            entry=row["close"],
            stop_basis=row["low"],
            atr=atr,
            timeframe=timeframe,
            reasons=(f"new_low_wick_trap={prev_low:.4f}", f"body_atr={body/atr:.2f}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    if row["high"] > prev_high and row.get("spec_upper_wick_ratio", 0.0) > 2.0:
        return SpecHit(
            strategy="wick_trap_reversal",
            direction="short",
            entry=row["close"],
            stop_basis=row["high"],
            atr=atr,
            timeframe=timeframe,
            reasons=(f"new_high_wick_trap={prev_high:.4f}", f"body_atr={body/atr:.2f}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    return None


def detect_volume_anomaly(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    volume_mean = row.get("spec_volume_mean20", 0.0)
    if atr <= 0.0 or volume_mean <= 0.0:
        return None
    if row["volume"] <= volume_mean * 3.0 or row.get("spec_body_ratio", 0.0) <= 0.6:
        return None
    direction = "long" if row["close"] > row["open"] else "short"
    stop_basis = row["low"] if direction == "long" else row["high"]
    return SpecHit(
        strategy="volume_anomaly",
        direction=direction,
        entry=row["close"],
        stop_basis=stop_basis,
        atr=atr,
        timeframe=timeframe,
        reasons=(f"volume_spike={row['volume']/volume_mean:.2f}x", f"body_ratio={row['spec_body_ratio']:.2f}"),
        structure_clarity=row.get("spec_body_ratio", 0.6),
        vol_ratio=row.get("volume_ratio20", row["volume"] / volume_mean),
        rsi=row.get("rsi14", 50.0),
    )


def detect_volume_climax_reversal(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 30:
        return None
    current_close = as_float(work.item(-1, "close"))
    current_idx = int(work.item(-1, "_spec_idx"))
    recent = work.tail(4).to_dicts()
    for row in reversed(recent[:-1]):
        idx = int(row["_spec_idx"])
        lag = current_idx - idx
        if lag < 1 or lag > 3:
            continue
        atr = as_float(row.get("spec_atr14"))
        volume_mean = as_float(row.get("spec_volume_mean20"))
        if atr <= 0.0 or volume_mean <= 0.0 or as_float(row.get("volume")) <= volume_mean * 5.0:
            continue
        midpoint = (as_float(row.get("high")) + as_float(row.get("low"))) / 2.0
        prev_high = as_float(row.get("spec_prev_high20"))
        prev_low = as_float(row.get("spec_prev_low20"))
        if as_float(row.get("low")) < prev_low and current_close > midpoint:
            return SpecHit(
                strategy="volume_climax_reversal",
                direction="long",
                entry=current_close,
                stop_basis=as_float(row.get("low")),
                atr=atr,
                timeframe=timeframe,
                reasons=(f"sell_climax_reclaimed_mid={midpoint:.4f}", f"lag={lag}"),
                vol_ratio=as_float(row.get("volume_ratio20"), 1.0),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
        if as_float(row.get("high")) > prev_high and current_close < midpoint:
            return SpecHit(
                strategy="volume_climax_reversal",
                direction="short",
                entry=current_close,
                stop_basis=as_float(row.get("high")),
                atr=atr,
                timeframe=timeframe,
                reasons=(f"buy_climax_reclaimed_mid={midpoint:.4f}", f"lag={lag}"),
                vol_ratio=as_float(row.get("volume_ratio20"), 1.0),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    return None


def detect_ema_bounce(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 55:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0 or row.get("spec_body_ratio", 0.0) <= 0.5:
        return None
    for period in (21, 50, 200):
        ema = row.get(f"spec_ema{period}", 0.0)
        if ema <= 0.0:
            continue
        if row["close"] > row["spec_ema200"] and row["low"] <= ema and row["close"] > ema:
            return SpecHit(
                strategy="ema_bounce",
                direction="long",
                entry=row["close"],
                stop_basis=row["low"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ema{period}_bounce", f"body_ratio={row['spec_body_ratio']:.2f}"),
                vol_ratio=row.get("volume_ratio20", 1.0),
                rsi=row.get("rsi14", 50.0),
            )
        if row["close"] < row["spec_ema200"] and row["high"] >= ema and row["close"] < ema:
            return SpecHit(
                strategy="ema_bounce",
                direction="short",
                entry=row["close"],
                stop_basis=row["high"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ema{period}_bounce", f"body_ratio={row['spec_body_ratio']:.2f}"),
                vol_ratio=row.get("volume_ratio20", 1.0),
                rsi=row.get("rsi14", 50.0),
            )
    return None


def detect_keltner_breakout(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    lookback_bars: int = 3,
    min_volume_ratio: float = 1.30,
    kc_atr_mult: float = 1.80,
    acceptance_band_pct: float = 0.01,
    min_body_ratio: float = 0.45,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    channel_mult = max(1.0, min(float(kc_atr_mult), 3.0))
    work = work.with_columns(
        [
            (pl.col("spec_ema20") + channel_mult * pl.col("spec_atr14")).alias(
                "spec_kc_break_upper"
            ),
            (pl.col("spec_ema20") - channel_mult * pl.col("spec_atr14")).alias(
                "spec_kc_break_lower"
            ),
        ]
    )
    current = _latest_values(work)
    current_close = current.get("close", 0.0)
    current_upper = current.get("spec_kc_break_upper", 0.0)
    current_lower = current.get("spec_kc_break_lower", 0.0)
    current_atr = current.get("spec_atr14", 0.0)
    if min(current_close, current_upper, current_atr) <= 0.0 or current_lower <= 0.0:
        return None
    recent_count = max(1, min(int(lookback_bars), 5, work.height))
    recent = work.tail(recent_count).to_dicts()
    current_idx = int(current.get("_spec_idx", work.height - 1))
    min_vol = max(0.0, float(min_volume_ratio))
    body_floor = max(0.0, min(float(min_body_ratio), 1.0))
    hold_band = max(0.0, min(float(acceptance_band_pct), 0.05))
    current_holds_long = current_close >= current_upper * (1.0 - hold_band)
    current_holds_short = current_close <= current_lower * (1.0 + hold_band)

    for row in reversed(recent):
        idx = int(row.get("_spec_idx", current_idx))
        lag = current_idx - idx
        if lag < 0 or lag >= recent_count:
            continue
        atr = as_float(row.get("spec_atr14"))
        upper = as_float(row.get("spec_kc_break_upper"))
        lower = as_float(row.get("spec_kc_break_lower"))
        volume_mean = as_float(row.get("spec_volume_mean20"))
        volume_ratio = as_float(row.get("volume_ratio20"), 0.0)
        if volume_ratio <= 0.0 and volume_mean > 0.0:
            volume_ratio = as_float(row.get("volume")) / volume_mean
        if atr <= 0.0 or upper <= 0.0 or lower <= 0.0 or volume_ratio < min_vol:
            continue
        body_ratio = as_float(row.get("spec_body_ratio"))
        open_price = as_float(row.get("open"))
        close_price = as_float(row.get("close"))
        directional_body = body_ratio >= body_floor
        if (
            current_holds_long
            and close_price > upper
            and close_price > open_price
            and directional_body
        ):
            return SpecHit(
                strategy="keltner_breakout",
                direction="long",
                entry=max(current_upper, upper),
                stop_basis=as_float(row.get("low")),
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"kc_upper_recent_break={upper:.4f}",
                    f"breakout_lag={lag}",
                    f"kc_mult={channel_mult:.2f}",
                    f"acceptance={current_close / current_upper:.4f}",
                ),
                vol_ratio=volume_ratio,
                rsi=current.get("rsi14", 50.0),
                source_index=idx,
            )
        if (
            current_holds_short
            and close_price < lower
            and close_price < open_price
            and directional_body
        ):
            return SpecHit(
                strategy="keltner_breakout",
                direction="short",
                entry=min(current_lower, lower),
                stop_basis=as_float(row.get("high")),
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"kc_lower_recent_break={lower:.4f}",
                    f"breakout_lag={lag}",
                    f"kc_mult={channel_mult:.2f}",
                    f"acceptance={current_close / current_lower:.4f}",
                ),
                vol_ratio=volume_ratio,
                rsi=current.get("rsi14", 50.0),
                source_index=idx,
            )
    return None


def detect_atr_expansion(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 20:
        return None
    row = _latest_values(work)
    baseline = row.get("spec_atr14", 0.0)
    tr = row.get("spec_tr", 0.0)
    if baseline <= 0.0 or tr / baseline <= 2.0:
        return None
    direction = "long" if row["close"] >= row["open"] else "short"
    return SpecHit(
        strategy="atr_expansion",
        direction=direction,
        entry=row["close"],
        stop_basis=row["low"] if direction == "long" else row["high"],
        atr=baseline,
        timeframe=timeframe,
        reasons=(f"atr_expansion_ratio={tr / baseline:.2f}",),
        structure_clarity=min(1.0, tr / baseline / 3.0),
        vol_ratio=row.get("volume_ratio20", 1.0),
        rsi=row.get("rsi14", 50.0),
    )


def detect_bb_squeeze_release(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 30:
        return None
    if "spec_squeeze" not in work.columns:
        LOGGER.warning("bb_squeeze_release missing spec_squeeze column")
        return None
    assert "spec_squeeze" in work.columns
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    try:
        was_squeeze = bool(work.item(-2, "spec_squeeze"))
        is_squeeze = bool(work.item(-1, "spec_squeeze"))
    except (IndexError, ValueError, TypeError):
        return None
    if not was_squeeze or is_squeeze:
        return None
    direction = "long" if row["close"] > row.get("spec_ema20", row["close"]) else "short"
    return SpecHit(
        strategy="bb_squeeze",
        direction=direction,
        entry=row["close"],
        stop_basis=row["low"] if direction == "long" else row["high"],
        atr=atr,
        timeframe=timeframe,
        reasons=("bb_kc_squeeze_released",),
        vol_ratio=row.get("volume_ratio20", 1.0),
        rsi=row.get("rsi14", 50.0),
    )


def detect_price_velocity(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    lookback: int = 5,
    threshold: float = 0.5,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < lookback + 20:
        return None
    close = as_float(work.item(-1, "close"))
    prior = as_float(work.item(-1 - lookback, "close"))
    atr = as_float(work.item(-1, "spec_atr14"))
    if min(close, prior, atr) <= 0.0:
        return None
    velocity_norm = ((close - prior) / lookback) / atr
    if abs(velocity_norm) <= threshold:
        return None
    direction = "long" if velocity_norm > 0.0 else "short"
    return SpecHit(
        strategy="price_velocity",
        direction=direction,
        entry=close,
        stop_basis=as_float(work.item(-1, "low" if direction == "long" else "high")),
        atr=atr,
        timeframe=timeframe,
        reasons=(f"velocity_norm={velocity_norm:.2f}",),
        structure_clarity=min(1.0, abs(velocity_norm)),
        vol_ratio=as_float(work.item(-1, "volume_ratio20"), 1.0),
        rsi=as_float(work.item(-1, "rsi14"), 50.0),
    )


def detect_vwap_reclaim(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    atr = as_float(work.item(-1, "spec_atr14"))
    if atr <= 0.0:
        return None
    prev_close = as_float(work.item(-2, "close"))
    prev_vwap = as_float(work.item(-2, "vwap"))
    close = as_float(work.item(-1, "close"))
    vwap = as_float(work.item(-1, "vwap"))
    if prev_close < prev_vwap and close > vwap:
        return SpecHit(
            strategy="vwap_trend",
            direction="long",
            entry=close,
            stop_basis=min(as_float(work.item(-1, "low")), vwap),
            atr=atr,
            timeframe=timeframe,
            reasons=(f"vwap_reclaim={vwap:.4f}",),
            vol_ratio=as_float(work.item(-1, "volume_ratio20"), 1.0),
            rsi=as_float(work.item(-1, "rsi14"), 50.0),
        )
    if prev_close > prev_vwap and close < vwap:
        return SpecHit(
            strategy="vwap_trend",
            direction="short",
            entry=close,
            stop_basis=max(as_float(work.item(-1, "high")), vwap),
            atr=atr,
            timeframe=timeframe,
            reasons=(f"vwap_reject={vwap:.4f}",),
            vol_ratio=as_float(work.item(-1, "volume_ratio20"), 1.0),
            rsi=as_float(work.item(-1, "rsi14"), 50.0),
        )
    return None


def detect_aggression_shift(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    delta = row.get("spec_delta", 0.0)
    delta_mean = row.get("spec_abs_delta_mean20", 0.0)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0 or delta_mean <= 0.0 or abs(delta) < delta_mean * 2.0:
        return None
    price_up = row["close"] > as_float(work.item(-2, "close"))
    if price_up and delta < 0.0:
        return SpecHit(
            strategy="aggression_shift",
            direction="short",
            entry=row["close"],
            stop_basis=row["high"],
            atr=atr,
            timeframe=timeframe,
            reasons=(f"bearish_delta_vs_price delta_x={abs(delta)/delta_mean:.2f}",),
            structure_clarity=min(1.0, abs(delta) / max(delta_mean * 3.0, 1e-8)),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    if not price_up and delta > 0.0:
        return SpecHit(
            strategy="aggression_shift",
            direction="long",
            entry=row["close"],
            stop_basis=row["low"],
            atr=atr,
            timeframe=timeframe,
            reasons=(f"bullish_delta_vs_price delta_x={abs(delta)/delta_mean:.2f}",),
            structure_clarity=min(1.0, abs(delta) / max(delta_mean * 3.0, 1e-8)),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    return None


def detect_absorption(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 26:
        return None
    prev = work.row(-2, named=True)
    latest = _latest_values(work)
    delta = finite_or_none(prev.get("spec_delta"))
    delta_mean = finite_or_none(prev.get("spec_abs_delta_mean20"))
    threshold_mult = 3.0
    delta_source = "spec_delta"
    if delta is None:
        prev_volume = as_float(prev.get("volume"))
        if prev_volume <= 0.0:
            return None
        delta = (as_float(prev.get("close")) - as_float(prev.get("open"))) / prev_volume
        proxy_values = [
            abs((as_float(row.get("close")) - as_float(row.get("open"))) / as_float(row.get("volume")))
            for row in work.tail(22).head(20).to_dicts()
            if as_float(row.get("volume")) > 0.0
        ]
        if not proxy_values:
            return None
        delta_mean = sum(proxy_values) / len(proxy_values)
        threshold_mult = 1.5
        delta_source = "ohlcv_body_volume_proxy"
    if delta_mean is None:
        return None
    atr = as_float(prev.get("spec_atr14"), latest.get("spec_atr14", 0.0))
    body = as_float(prev.get("spec_body"))
    if atr <= 0.0 or delta_mean <= 0.0:
        return None
    absorbed = abs(delta) > delta_mean * threshold_mult and body < atr * 0.3
    if not absorbed:
        return None
    prev_close = as_float(prev.get("close"))
    if delta < 0.0 and latest["close"] > prev_close:
        return SpecHit(
            strategy="absorption",
            direction="long",
            entry=latest["close"],
            stop_basis=as_float(prev.get("low")),
            atr=atr,
            timeframe=timeframe,
            reasons=(
                f"sell_delta_absorbed delta_x={abs(delta)/delta_mean:.2f}",
                f"delta_source={delta_source}",
            ),
            structure_clarity=min(1.0, abs(delta) / max(delta_mean * 4.0, 1e-8)),
            vol_ratio=latest.get("volume_ratio20", 1.0),
            rsi=latest.get("rsi14", 50.0),
        )
    if delta > 0.0 and latest["close"] < prev_close:
        return SpecHit(
            strategy="absorption",
            direction="short",
            entry=latest["close"],
            stop_basis=as_float(prev.get("high")),
            atr=atr,
            timeframe=timeframe,
            reasons=(
                f"buy_delta_absorbed delta_x={abs(delta)/delta_mean:.2f}",
                f"delta_source={delta_source}",
            ),
            structure_clarity=min(1.0, abs(delta) / max(delta_mean * 4.0, 1e-8)),
            vol_ratio=latest.get("volume_ratio20", 1.0),
            rsi=latest.get("rsi14", 50.0),
        )
    return None


def detect_regular_divergence(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    require_oversold: bool = False,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 60:
        return None
    atr = as_float(work.item(-1, "spec_atr14"))
    if atr <= 0.0:
        return None
    lows = _pivot_rows(work, price_column="low", indicator_column="rsi14", pivot="low")
    if len(lows) >= 2:
        old, new = lows[-2], lows[-1]
        if new["price"] < old["price"] and new["indicator"] > old["indicator"]:
            if not require_oversold or min(old["indicator"], new["indicator"]) < 35.0:
                strategy = "rsi_divergence_bottom" if require_oversold else "indicator_divergence"
                return SpecHit(
                    strategy=strategy,
                    direction="long",
                    entry=as_float(work.item(-1, "close")),
                    stop_basis=new["price"],
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(
                        f"regular_bullish_div price_ll={new['price']:.4f}",
                        f"rsi_hl={new['indicator']:.1f}",
                    ),
                    rsi=as_float(work.item(-1, "rsi14"), 50.0),
                )
    highs = _pivot_rows(work, price_column="high", indicator_column="rsi14", pivot="high")
    if len(highs) >= 2 and not require_oversold:
        old, new = highs[-2], highs[-1]
        if new["price"] > old["price"] and new["indicator"] < old["indicator"]:
            return SpecHit(
                strategy="indicator_divergence",
                direction="short",
                entry=as_float(work.item(-1, "close")),
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"regular_bearish_div price_hh={new['price']:.4f}",
                    f"rsi_lh={new['indicator']:.1f}",
                ),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    return None


def detect_hidden_divergence(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 60:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    min_rsi_separation = 4.0
    lows = _pivot_rows(work, price_column="low", indicator_column="rsi14", pivot="low")
    if row["close"] > row.get("spec_ema50", row["close"]) and len(lows) >= 2:
        old, new = lows[-2], lows[-1]
        rsi_gap = old["indicator"] - new["indicator"]
        if (
            new["price"] > old["price"]
            and rsi_gap >= min_rsi_separation
        ):
            return SpecHit(
                strategy="hidden_divergence",
                direction="long",
                entry=row["close"],
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"hidden_bullish_div price_hl={new['price']:.4f}",
                    f"rsi_ll_gap={rsi_gap:.2f}",
                ),
                rsi=row.get("rsi14", 50.0),
            )
    highs = _pivot_rows(work, price_column="high", indicator_column="rsi14", pivot="high")
    if row["close"] < row.get("spec_ema50", row["close"]) and len(highs) >= 2:
        old, new = highs[-2], highs[-1]
        rsi_gap = new["indicator"] - old["indicator"]
        if (
            new["price"] < old["price"]
            and rsi_gap >= min_rsi_separation
        ):
            return SpecHit(
                strategy="hidden_divergence",
                direction="short",
                entry=row["close"],
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"hidden_bearish_div price_lh={new['price']:.4f}",
                    f"rsi_hh_gap={rsi_gap:.2f}",
                ),
                rsi=row.get("rsi14", 50.0),
            )
    return None


def detect_cvd_divergence(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 60:
        return None
    atr = as_float(work.item(-1, "spec_atr14"))
    delta_std = as_float(work.item(-1, "spec_delta_std20"))
    if atr <= 0.0 or delta_std <= 0.0:
        return None
    lows = _pivot_rows(work, price_column="low", indicator_column="spec_cvd", pivot="low")
    if len(lows) >= 2:
        old, new = lows[-2], lows[-1]
        cvd_shift = new["indicator"] - old["indicator"]
        if new["price"] < old["price"] and cvd_shift > 2.0 * delta_std:
            return SpecHit(
                strategy="cvd_divergence",
                direction="long",
                entry=as_float(work.item(-1, "close")),
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"price_ll_cvd_hl shift={cvd_shift:.4f}",),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    highs = _pivot_rows(work, price_column="high", indicator_column="spec_cvd", pivot="high")
    if len(highs) >= 2:
        old, new = highs[-2], highs[-1]
        cvd_shift = old["indicator"] - new["indicator"]
        if new["price"] > old["price"] and cvd_shift > 2.0 * delta_std:
            return SpecHit(
                strategy="cvd_divergence",
                direction="short",
                entry=as_float(work.item(-1, "close")),
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"price_hh_cvd_lh shift={cvd_shift:.4f}",),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    return None


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

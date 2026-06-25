"""Shared spec detector primitives."""

from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import polars as pl

from bot.policy.catalog_guards import catalog_allows_signal
from engine.domain.strategy_catalog import catalog_default_params
from engine.features.pivots import (
    _pivot_rows,
    as_float,
    required_columns,
    with_spec_columns,
)

from ..setups import _build_signal, _compute_dynamic_score, _reject

if TYPE_CHECKING:
    from engine.domain.config import BotSettings
    from engine.domain.schemas import PreparedSymbol, Signal

LOGGER = logging.getLogger(__name__)

__all__ = [
    "SpecHit",
    "_pivot_rows",
    "as_float",
    "required_columns",
    "with_spec_columns",
]


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
    entry_order_type: str = "limit"


def finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def first_finite(*values: object) -> float | None:
    for value in values:
        numeric = finite_or_none(value)
        if numeric is not None:
            return numeric
    return None


def last(frame: pl.DataFrame, column: str, default: float = 0.0) -> float:
    if frame.is_empty() or column not in frame.columns:
        return default
    source = confirmed_pattern_frame(frame)
    if source.is_empty():
        return default
    return as_float(source.item(-1, column), default)


def previous(frame: pl.DataFrame, column: str, default: float = 0.0) -> float:
    if frame.height < 2 or column not in frame.columns:
        return default
    return as_float(frame.item(-2, column), default)


def last_bar_is_closed(frame: pl.DataFrame) -> bool:
    """True when the frame tail is a fully closed bar (not a live forming candle)."""
    if frame.is_empty():
        return False
    if "is_closed" not in frame.columns:
        return True
    value = frame["is_closed"].item(-1)
    if value is None:
        return True
    return bool(value)


def confirmed_pattern_frame(frame: pl.DataFrame) -> pl.DataFrame:
    """Return a frame whose last row is the latest closed bar (fix-sl-A)."""
    if frame.is_empty() or last_bar_is_closed(frame):
        return frame
    if frame.height < 2:
        return frame.head(0)
    return frame.head(frame.height - 1)


def is_confirmed_bar(frame: pl.DataFrame) -> bool:
    """Whether pattern logic can use a closed bar (not a forming candle tail)."""
    return confirmed_pattern_frame(frame).height >= 1


def bar_value(
    frame: pl.DataFrame,
    column: str,
    *,
    confirmed: bool = False,
    default: float = 0.0,
) -> float:
    """Read last (or last closed) bar value for strategy pattern helpers."""
    source = confirmed_pattern_frame(frame) if confirmed else frame
    return last(source, column, default)


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


def build_spec_signal(
    *,
    prepared: PreparedSymbol,
    _settings: BotSettings,
    setup_id: str,
    family: str,
    hit: SpecHit,
    defaults: dict[str, float],
    params: dict[str, float] | None = None,
    entry_order_type: str = "limit",
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
    effective_order_type = str(hit.entry_order_type or entry_order_type or "limit").strip().lower()
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
        entry_order_type=effective_order_type,
    )


def _latest_values(work: pl.DataFrame) -> dict[str, float]:
    if work.is_empty():
        return {}
    last = work.row(-1, named=True)
    return {key: as_float(value) for key, value in last.items() if isinstance(key, str)}


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
    for prev, row in itertools.pairwise(rows):
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
        return datetime.now(UTC).hour
    for column in ("open_time", "time", "close_time"):
        if column not in frame.columns:
            continue
        value = frame.item(-1, column)
        if isinstance(value, datetime):
            dt = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
            return dt.hour
    return datetime.now(UTC).hour


# --- merged from common.py ---
def orderflow_supports_reversal(
    prepared: object,
    direction: str,
    *,
    min_delta_long: float = 0.49,
    max_delta_short: float = 0.51,
    max_adverse_depth: float = 0.08,
    max_adverse_micro: float = 0.08,
) -> tuple[bool, dict[str, float]]:
    """Confirm reversal direction with delta / book microstructure (ICT-style recovery)."""
    depth = finite_or_none(getattr(prepared, "depth_imbalance", None))
    micro = finite_or_none(getattr(prepared, "microprice_bias", None))
    delta_ratio: float | None = None
    work = getattr(prepared, "work_15m", None)
    if work is not None and not work.is_empty() and "delta_ratio" in work.columns:
        delta_ratio = finite_or_none(work.item(-1, "delta_ratio"))
    if delta_ratio is None:
        agg = finite_or_none(getattr(prepared, "agg_trade_delta_30s", None))
        if agg is not None:
            delta_ratio = 0.5 + max(-0.5, min(0.5, agg / 2.0))

    details: dict[str, float] = {}
    if delta_ratio is not None:
        details["delta_ratio"] = delta_ratio
    if depth is not None:
        details["depth_imbalance"] = depth
    if micro is not None:
        details["microprice_bias"] = micro

    if direction == "long":
        if delta_ratio is not None and delta_ratio < min_delta_long:
            return False, details
        if depth is not None and depth <= -max_adverse_depth:
            return False, details
        if micro is not None and micro <= -max_adverse_micro:
            return False, details
    else:
        if delta_ratio is not None and delta_ratio > max_delta_short:
            return False, details
        if depth is not None and depth >= max_adverse_depth:
            return False, details
        if micro is not None and micro >= max_adverse_micro:
            return False, details
    return True, details

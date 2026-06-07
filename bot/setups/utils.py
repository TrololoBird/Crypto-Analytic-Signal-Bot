"""Unified utilities for setup SL/TP calculation and graded scoring.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..domain.risk import RiskParams
from ..domain.strategy_catalog import catalog_default_params
from ..runtime_policy import is_deep_analysis_symbol

if TYPE_CHECKING:
    import polars as pl

    from ..domain.schemas import PreparedSymbol, Signal


def coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def coerce_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return float(value)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _safe_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _is_finite_positive(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value)) and float(value) > 0.0


def _price_tolerance(price_anchor: float) -> float:
    return max(abs(float(price_anchor)) * 1e-8, 1e-8)


def normalize_target_pair(
    direction: str,
    price_anchor: float,
    tp1: float,
    tp2: float,
) -> tuple[float, float, bool, str] | None:
    """Normalize TP ordering and detect single-target semantics."""
    if not _is_finite_positive(price_anchor):
        return None
    if not _is_finite_positive(tp1) or not _is_finite_positive(tp2):
        return None

    target_a = float(tp1)
    target_b = float(tp2)
    tolerance = _price_tolerance(price_anchor)

    if direction == "long":
        ordered_tp1, ordered_tp2 = sorted((target_a, target_b))
        if ordered_tp1 <= price_anchor + tolerance or ordered_tp2 <= price_anchor + tolerance:
            return None
    elif direction == "short":
        ordered_tp1, ordered_tp2 = sorted((target_a, target_b), reverse=True)
        if ordered_tp1 >= price_anchor - tolerance or ordered_tp2 >= price_anchor - tolerance:
            return None
    else:
        return None

    normalized = not (
        math.isclose(target_a, ordered_tp1, abs_tol=tolerance, rel_tol=0.0)
        and math.isclose(target_b, ordered_tp2, abs_tol=tolerance, rel_tol=0.0)
    )
    single_target_mode = math.isclose(ordered_tp1, ordered_tp2, abs_tol=tolerance, rel_tol=0.0)
    if single_target_mode:
        ordered_tp1 = ordered_tp2

    if normalized and single_target_mode:
        status = "normalized_single_target"
    elif normalized:
        status = "normalized"
    elif single_target_mode:
        status = "single_target"
    else:
        status = "valid"
    return ordered_tp1, ordered_tp2, single_target_mode, status


def normalize_trade_levels(
    direction: str,
    price_anchor: float,
    stop: float,
    tp1: float,
    tp2: float,
) -> tuple[float, float, float, bool, str] | None:
    """Validate mirrored target invariant and normalize target ordering."""
    if not _is_finite_positive(price_anchor) or not _is_finite_positive(stop):
        return None

    normalized_targets = normalize_target_pair(direction, price_anchor, tp1, tp2)
    if normalized_targets is None:
        return None
    normalized_tp1, normalized_tp2, single_target_mode, status = normalized_targets
    tolerance = _price_tolerance(price_anchor)
    normalized_stop = float(stop)

    if direction == "long":
        if normalized_stop >= price_anchor - tolerance:
            return None
    elif direction == "short":
        if normalized_stop <= price_anchor + tolerance:
            return None
    else:
        return None

    return normalized_stop, normalized_tp1, normalized_tp2, single_target_mode, status


def select_structural_target(
    work: pl.DataFrame,
    *,
    mask: pl.Series | None,
    column: str,
    price_anchor: float,
    direction: str,
    max_age_bars: int = 48,
    max_retests: int = 5,
) -> float | None:
    if mask is None or work.is_empty() or column not in work.columns or mask.sum() <= 0:
        return None

    prices = work.filter(mask)[column]
    mask_indices = work.with_row_index("_idx").filter(mask)["_idx"]
    best_price: float | None = None
    best_score: float | None = None
    total_bars = work.height

    for i, raw_price in enumerate(prices.to_list()):
        price = _safe_float(raw_price, default=float("nan"))
        if math.isnan(price) or price <= 0.0:
            continue
        if direction == "long" and price <= price_anchor:
            continue
        if direction == "short" and price >= price_anchor:
            continue

        bar_idx = int(mask_indices[i]) if i < len(mask_indices) else 0
        age_bars = total_bars - bar_idx
        if age_bars > max_age_bars:
            continue

        retests = 0
        high_col = work["high"] if "high" in work.columns else None
        low_col = work["low"] if "low" in work.columns else None
        if high_col is not None and low_col is not None:
            zone = price * 0.003
            for j in range(bar_idx, total_bars):
                if j >= total_bars:
                    break
                h = _safe_float(high_col[j], default=float("nan"))
                l_ = _safe_float(low_col[j], default=float("nan"))
                if (
                    math.isfinite(h) and math.isfinite(l_)
                    and l_ <= price + zone and h >= price - zone
                ):
                    retests += 1
            if retests > max_retests:
                continue

        distance = abs(price - price_anchor)
        age_penalty = age_bars / max_age_bars
        retest_penalty = min(retests / max_retests, 1.0)
        score = distance * (1.0 + age_penalty * 0.5 + retest_penalty * 0.3)
        if best_score is None or score <= best_score:
            best_price = price
            best_score = score
    return best_price


def select_structural_stop_anchor(
    work: pl.DataFrame,
    *,
    sh_mask: pl.Series | None,
    sl_mask: pl.Series | None,
    price_anchor: float,
    stop_basis: float,
    direction: str,
    vwap: float | None = None,
    volume_profile_poc: float | None = None,
    volume_profile_vah: float | None = None,
    volume_profile_val: float | None = None,
) -> float:
    anchor = float(stop_basis)

    additional_levels: list[float] = []
    if vwap is not None and math.isfinite(vwap):
        additional_levels.append(vwap)
    if volume_profile_poc is not None and math.isfinite(volume_profile_poc):
        additional_levels.append(volume_profile_poc)
    if direction == "long":
        if volume_profile_val is not None and math.isfinite(volume_profile_val):
            additional_levels.append(volume_profile_val)
    else:
        if volume_profile_vah is not None and math.isfinite(volume_profile_vah):
            additional_levels.append(volume_profile_vah)

    if direction == "long":
        structural_support = select_structural_target(
            work,
            mask=sl_mask,
            column="low",
            price_anchor=price_anchor,
            direction="short",
        )
        if structural_support is not None:
            anchor = min(anchor, structural_support)
        for level in additional_levels:
            if level < price_anchor and level > anchor * 0.95:
                anchor = min(anchor, level)
        return anchor
    structural_resistance = select_structural_target(
        work,
        mask=sh_mask,
        column="high",
        price_anchor=price_anchor,
        direction="long",
    )
    if structural_resistance is not None:
        anchor = max(anchor, structural_resistance)
    for level in additional_levels:
        if level > price_anchor and level < anchor * 1.05:
            anchor = max(anchor, level)
    return max(anchor, price_anchor)


def build_structural_targets(
    direction: str,
    price_anchor: float,
    stop_basis: float,
    atr: float,
    work_1h: pl.DataFrame,
    work_4h: pl.DataFrame | None = None,
    min_rr: float = 1.5,
    sl_buffer_atr: float = 1.5,
    sh_mask: pl.Series | None = None,
    sl_mask: pl.Series | None = None,
    breakout_bar_idx: int | None = None,
    broken_level: float | None = None,
) -> tuple[float, float | None, float | None]:
    """Calculate unified structural SL/TP targets.

    Args:
        direction: 'long' or 'short'
        price_anchor: Entry price reference
        stop_basis: Base level for SL calculation
        atr: Current ATR for buffer calculation
        work_1h: 1H timeframe data for TP calculation
        work_4h: Optional 4H timeframe for extended targets
        min_rr: Minimum risk/reward ratio
        sl_buffer_atr: ATR multiplier used as stop noise buffer around stop_basis
        sh_mask: Swing high boolean mask (for long TP1)
        sl_mask: Swing low boolean mask (for short TP1)
        breakout_bar_idx: Index of breakout bar (for TP2 measured move)
        broken_level: Level that was broken (for TP2 projection)

    Returns:
        Tuple of (stop, tp1, tp2) where tp1/tp2 may be None
    """
    stop_buffer = max(0.05, float(sl_buffer_atr))
    _cols = work_1h.columns
    _vwap = _safe_float(work_1h["vwap"][-1]) if "vwap" in _cols else None
    _vp_poc = _safe_float(work_1h["volume_profile"][-1]) if "volume_profile" in _cols else None
    _vp_vah = (
        _safe_float(work_1h["volume_profile_vah"][-1])
        if "volume_profile_vah" in _cols else None
    )
    _vp_val = (
        _safe_float(work_1h["volume_profile_val"][-1])
        if "volume_profile_val" in _cols else None
    )
    stop_anchor = select_structural_stop_anchor(
        work_1h,
        sh_mask=sh_mask,
        sl_mask=sl_mask,
        price_anchor=price_anchor,
        stop_basis=stop_basis,
        direction=direction,
        vwap=_vwap,
        volume_profile_poc=_vp_poc,
        volume_profile_vah=_vp_vah,
        volume_profile_val=_vp_val,
    )

    # Extract daily pivot levels as additional TP/SL candidates
    _pp = _safe_float(work_1h["pivot_point"][-1]) if "pivot_point" in work_1h.columns else None
    _r1 = _safe_float(work_1h["r1"][-1]) if "r1" in work_1h.columns else None
    _r2 = _safe_float(work_1h["r2"][-1]) if "r2" in work_1h.columns else None
    _s1 = _safe_float(work_1h["s1"][-1]) if "s1" in work_1h.columns else None
    _s2 = _safe_float(work_1h["s2"][-1]) if "s2" in work_1h.columns else None

    if direction == "long":
        # SL: below stop_basis + configurable ATR noise buffer.
        stop = stop_anchor - atr * stop_buffer

        # TP1: nearest resistance — swing high or daily pivot R1/R2 above entry
        tp1 = None
        if sh_mask is not None and sh_mask.sum() > 0:
            tp1 = select_structural_target(
                work_1h,
                mask=sh_mask,
                column="high",
                price_anchor=price_anchor,
                direction="long",
            )
        # Pivot R1/R2 as TP1 candidate — pick nearest above entry
        pivot_tp1_candidates = [
            lvl for lvl in (_r1, _r2) if lvl is not None and lvl > price_anchor
        ]
        if pivot_tp1_candidates:
            pivot_tp1 = min(pivot_tp1_candidates)
            if tp1 is None or (pivot_tp1 < tp1 and pivot_tp1 > price_anchor):
                tp1 = pivot_tp1

        # TP2: measured move = prior range projected from breakout point
        tp2 = None
        if breakout_bar_idx is not None and breakout_bar_idx > 0 and broken_level is not None:
            high_before = _safe_float(work_1h["high"].slice(0, breakout_bar_idx).max())
            low_before = _safe_float(work_1h["low"].slice(0, breakout_bar_idx).min())
            range_before = float(high_before - low_before)
            tp2 = broken_level + range_before
        elif work_4h is not None and not work_4h.is_empty():
            # Fallback to 4H resistance if no measured move
            last_4h_high = float(work_4h["high"][-1])
            if last_4h_high > price_anchor * 1.02:  # At least 2% above
                tp2 = last_4h_high
        # R2 as TP2 candidate if still missing
        if tp2 is None and _r2 is not None and _r2 > price_anchor:
            tp2 = _r2
    else:
        # SL: above stop_basis + configurable ATR noise buffer.
        stop = stop_anchor + atr * stop_buffer

        # TP1: nearest support — swing low or daily pivot S1/S2 below entry
        tp1 = None
        if sl_mask is not None and sl_mask.sum() > 0:
            tp1 = select_structural_target(
                work_1h,
                mask=sl_mask,
                column="low",
                price_anchor=price_anchor,
                direction="short",
            )
        # Pivot S1/S2 as TP1 candidate — pick nearest below entry
        pivot_tp1_candidates = [
            lvl for lvl in (_s1, _s2) if lvl is not None and lvl < price_anchor
        ]
        if pivot_tp1_candidates:
            pivot_tp1 = max(pivot_tp1_candidates)
            if tp1 is None or (pivot_tp1 > tp1 and pivot_tp1 < price_anchor):
                tp1 = pivot_tp1

        # TP2: measured move downward from breakout point
        tp2 = None
        if breakout_bar_idx is not None and breakout_bar_idx > 0 and broken_level is not None:
            high_before = _safe_float(work_1h["high"].slice(0, breakout_bar_idx).max())
            low_before = _safe_float(work_1h["low"].slice(0, breakout_bar_idx).min())
            range_before = float(high_before - low_before)
            tp2 = broken_level - range_before
        elif work_4h is not None and not work_4h.is_empty():
            # Fallback to 4H support if no measured move
            last_4h_low = float(work_4h["low"][-1])
            if last_4h_low < price_anchor * 0.98:  # At least 2% below
                tp2 = last_4h_low
        # S2 as TP2 candidate if still missing
        if tp2 is None and _s2 is not None and _s2 < price_anchor:
            tp2 = _s2

    # Fallback: if tp2 is None, project a second target beyond TP1.
    if tp2 is None:
        risk = abs(price_anchor - stop)
        if risk > 0.0:
            tp2 = (
                price_anchor + risk * max(2.0, min_rr + 0.35)
                if direction == "long"
                else price_anchor - risk * max(2.0, min_rr + 0.35)
            )
    if tp1 is not None and tp2 is not None:
        normalized_targets = normalize_target_pair(direction, price_anchor, tp1, tp2)
        if normalized_targets is None:
            tp1 = None
            tp2 = None
        else:
            tp1, tp2, _, _ = normalized_targets

    return stop, tp1, tp2


@dataclass(frozen=True, slots=True)
class SMCTradePlan:
    entry: float
    stop: float
    tp1: float
    tp2: float
    risk: float
    reasons_note: str


def build_smc_trade_plan(
    *,
    direction: str,
    price_anchor: float,
    stop_basis: float,
    atr: float,
    work_1h: pl.DataFrame,
    work_4h: pl.DataFrame | None = None,
    min_rr: float = 1.9,
    sl_buffer_atr: float = 0.8,
    sh_mask: pl.Series | None = None,
    sl_mask: pl.Series | None = None,
    tp2_extension_rr: float = 0.35,
) -> SMCTradePlan | None:
    """SMC continuation/reversal SL/TP plan with structural targets and RR fallback."""
    if not _is_finite_positive(price_anchor) or not _is_finite_positive(stop_basis):
        return None
    if not math.isfinite(float(atr)) or float(atr) <= 0.0:
        return None

    buffer = max(0.05, float(sl_buffer_atr))
    min_rr_value = max(1.0, float(min_rr))
    extension = max(2.0, min_rr_value + float(tp2_extension_rr))

    if direction == "long":
        stop = float(stop_basis) - float(atr) * buffer
        risk = price_anchor - stop
        if risk <= 0.0:
            return None
        tp1 = select_structural_target(
            work_1h,
            mask=sh_mask,
            column="high",
            price_anchor=price_anchor,
            direction="long",
        )
        tp2 = None
        if work_4h is not None and not work_4h.is_empty() and "high" in work_4h.columns:
            tp2 = select_structural_target(
                work_4h,
                mask=None,
                column="high",
                price_anchor=price_anchor,
                direction="long",
            )
        if tp1 is None or abs(tp1 - price_anchor) < risk * min_rr_value:
            tp1 = price_anchor + risk * min_rr_value
            reasons_note = "tp1_rr_fallback"
        else:
            reasons_note = "tp1_structural"
        if tp2 is None or tp2 <= tp1:
            tp2 = price_anchor + risk * extension
    elif direction == "short":
        stop = float(stop_basis) + float(atr) * buffer
        risk = stop - price_anchor
        if risk <= 0.0:
            return None
        tp1 = select_structural_target(
            work_1h,
            mask=sl_mask,
            column="low",
            price_anchor=price_anchor,
            direction="short",
        )
        tp2 = None
        if work_4h is not None and not work_4h.is_empty() and "low" in work_4h.columns:
            tp2 = select_structural_target(
                work_4h,
                mask=None,
                column="low",
                price_anchor=price_anchor,
                direction="short",
            )
        if tp1 is None or abs(tp1 - price_anchor) < risk * min_rr_value:
            tp1 = price_anchor - risk * min_rr_value
            reasons_note = "tp1_rr_fallback"
        else:
            reasons_note = "tp1_structural"
        if tp2 is None or tp2 >= tp1:
            tp2 = price_anchor - risk * extension
    else:
        return None

    if abs(tp2 - price_anchor) <= abs(tp1 - price_anchor):
        tp2 = (
            price_anchor + risk * extension
            if direction == "long"
            else price_anchor - risk * extension
        )

    return SMCTradePlan(
        entry=price_anchor,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        risk=risk,
        reasons_note=reasons_note,
    )


def validate_rr_or_penalty(
    price_anchor: float,
    stop: float,
    tp1: float | None,
    min_rr: float = 1.5,
) -> tuple[bool, float | None]:
    """Validate risk/reward ratio and return (is_valid, adjusted_tp1).

    Returns:
        Tuple of (is_valid, tp1_or_none). If RR < min_rr, returns (False, None).
        Caller should apply penalty to score instead of rejecting signal.
    """
    if tp1 is None:
        return False, None

    risk = abs(price_anchor - stop)
    if risk <= 0:
        return False, None

    direction = "long" if stop < price_anchor else "short"
    risk_params = RiskParams(
        entry=price_anchor,
        stop=stop,
        tp1=tp1,
        tp2=tp1,
        tp3=tp1,
        direction=direction,
    )
    if risk_params.rr1() + 1e-9 < float(min_rr):
        return False, tp1

    return True, tp1


def apply_graded_penalty(
    signal: Signal,
    *,
    condition: bool,
    penalty: float = 0.75,
    reason: str = "",
) -> Signal:
    """Apply graded penalty to signal score if condition is True.

    Instead of rejecting signal completely, reduce score by penalty factor.
    This allows more signals to pass through with lower confidence.

    Args:
        signal: Signal to potentially penalize
        condition: If True, apply penalty
        penalty: Multiplier for score (0.75 = reduce by 25%)
        reason: Reason for penalty (added to signal reasons)

    Returns:
        Modified signal with updated score and reasons
    """
    if not condition or signal.score <= 0:
        return signal

    reasons = signal.reasons
    if reason and reason not in reasons:
        reasons = (*reasons, reason)
    return replace(signal, score=signal.score * penalty, reasons=reasons)


def _relax_deep_asset_thresholds(
    prepared: PreparedSymbol,
    params: dict[str, float],
) -> dict[str, float]:
    if not params or not is_deep_analysis_symbol(prepared):
        return params

    adjusted = dict(params)
    primary_timeframe = str(getattr(prepared, "primary_timeframe", "15m") or "15m")
    floor = 0.45 if primary_timeframe in {"1h", "4h"} else 0.55
    cap = 0.75 if primary_timeframe in {"1h", "4h"} else 0.85
    for key in ("min_volume_ratio", "volume_threshold"):
        raw_value = adjusted.get(key)
        if not isinstance(raw_value, int | float) or not math.isfinite(float(raw_value)):
            continue
        value = float(raw_value)
        if value <= floor:
            continue
        adjusted[key] = max(floor, min(cap, value * 0.65))
    return adjusted


def get_dynamic_params(prepared: PreparedSymbol, setup_id: str) -> dict[str, float]:
    """Get dynamic parameters from prepared symbol for specific setup.

    This retrieves setup-specific configuration from settings or falls back
    to defaults. Used for making base_score and thresholds configurable.

    Args:
        prepared: Prepared symbol with attached settings
        setup_id: Setup identifier (e.g., 'ema_bounce', 'fvg')

    Returns:
        Dictionary of dynamic parameters
    """
    # Try to get from prepared.settings if available
    settings = prepared.settings
    if settings is None:
        return {}

    # Get setup-specific filters from settings
    filters = settings.filters
    if filters is None:
        return {}

    # Try to get setup-specific config
    setups_config = filters.setups
    params: dict[str, float] = {}
    if isinstance(setups_config, dict) and setup_id in setups_config:
        params = dict(setups_config.get(setup_id, {}) or {})
    elif isinstance(setups_config, dict) and setup_id.endswith("_setup"):
        legacy_key = setup_id.removesuffix("_setup")
        if legacy_key in setups_config:
            params = dict(setups_config.get(legacy_key, {}) or {})

    merged = {**catalog_default_params(setup_id), **params}
    return _relax_deep_asset_thresholds(prepared, merged)

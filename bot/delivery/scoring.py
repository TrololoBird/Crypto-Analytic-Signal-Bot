"""Simplified structure-based scoring engine."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import polars as pl

from ..features.prepare import _swing_points

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.scoring")
_FUNDING_DEFAULT_WARNING_STATE: dict[str, bool] = {"emitted": False}
_DEFAULT_FUNDING_RATE_EXTREME = 0.001
_DEFAULT_FUNDING_RATE_MODERATE = 0.0005


@dataclass(frozen=True, slots=True)
class ScoringResult:
    base_score: float
    adjustments: dict[str, float]
    final_score: float
    setup_id: str = ""
    ml_multiplier: float | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def total_adjustment(self) -> float:
        raw_adjustment = self.final_score - self.base_score
        return max(-0.5, min(0.5, raw_adjustment))  # clamp: adjustment cannot flip signal direction

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "base_score": self.base_score,
            "adjustments": self.adjustments,
            "final_score": self.final_score,
            "setup_id": self.setup_id,
            "ml_multiplier": self.ml_multiplier,
        }
        if self.notes:
            payload["notes"] = self.notes
        return payload


def _directional_alignment(value: str, direction: str) -> float:
    target = "uptrend" if direction == "long" else "downtrend"
    opposite = "downtrend" if direction == "long" else "uptrend"
    if value == target:
        return 1.0
    if value == opposite:
        return 0.0
    return 0.5


def _regime_confirmed(work: pl.DataFrame, min_bars: int = 3) -> str:
    """EMA-aligned regime from any prepared frame."""
    if len(work) < min_bars:
        return "ranging"
    if not all(column in work.columns for column in ("ema20", "ema50", "ema200")):
        return "ranging"
    tail = work.tail(min_bars)
    uptrend_count = tail.filter(
        (pl.col("ema20") > pl.col("ema50")) & (pl.col("ema50") > pl.col("ema200"))
    ).height
    downtrend_count = tail.filter(
        (pl.col("ema20") < pl.col("ema50")) & (pl.col("ema50") < pl.col("ema200"))
    ).height
    if uptrend_count == min_bars:
        return "uptrend"
    if downtrend_count == min_bars:
        return "downtrend"
    return "ranging"


def _structure_frame(prepared: PreparedSymbol) -> pl.DataFrame:
    if prepared.primary_timeframe != "15m":
        primary = getattr(prepared, "work_primary", None)
        if isinstance(primary, pl.DataFrame) and not primary.is_empty():
            return primary
    return prepared.work_1h


def _structure_poc(prepared: PreparedSymbol) -> float | None:
    if prepared.primary_timeframe == "15m":
        return prepared.poc_1h
    if prepared.primary_timeframe == "1h":
        return prepared.poc_1h
    return prepared.poc_1h


def _mtf_alignment(prepared: PreparedSymbol, signal: Signal) -> float:
    score_4h = _directional_alignment(prepared.regime_4h_confirmed, signal.direction)
    if prepared.primary_timeframe != "15m":
        primary = _structure_frame(prepared)
        score_primary = _directional_alignment(_regime_confirmed(primary), signal.direction)
    else:
        score_primary = _directional_alignment(prepared.structure_1h, signal.direction)
    # Weighted average: primary structure/regime is direct context (70%), 4h bias is context (30%).
    return max(0.0, min(score_primary * 0.70 + score_4h * 0.30, 1.0))


def _volume_quality(prepared: PreparedSymbol) -> float:
    primary = getattr(prepared, "work_primary", None)
    frame = primary if primary is not None and not primary.is_empty() else prepared.work_15m
    if frame.is_empty() or "volume_ratio20" not in frame.columns:
        return 0.0
    ratio = float(frame.item(-1, "volume_ratio20") or 0.0)
    return max(0.0, min(max(ratio, 0.5) / 1.5, 1.0))


def _nearest_structure_level(prepared: PreparedSymbol, signal: Signal) -> float | None:
    frame = _structure_frame(prepared)
    if frame.is_empty():
        return None
    levels: list[float] = []
    if "ema20" in frame.columns:
        ema20 = float(frame.item(-1, "ema20") or 0.0)
        if ema20 > 0.0:
            levels.append(ema20)
    poc = _structure_poc(prepared)
    if poc and poc > 0.0:
        levels.append(poc)
    sh_mask, sl_mask = _swing_points(frame, n=3)
    if signal.direction == "long":
        swing_levels = frame.filter(sl_mask)["low"].tail(3).to_list()
    else:
        swing_levels = frame.filter(sh_mask)["high"].tail(3).to_list()
    levels.extend(float(level) for level in swing_levels if float(level) > 0.0)
    if not levels:
        return None
    anchor = signal.entry_mid
    return min(levels, key=lambda level: abs(level - anchor))


def _structure_clarity(prepared: PreparedSymbol, signal: Signal) -> float:
    frame = _structure_frame(prepared)
    if frame.is_empty():
        return 0.0
    level = _nearest_structure_level(prepared, signal)
    if level is None or level <= 0.0:
        return 0.0
    atr = float(frame.item(-1, "atr14") or 0.0) if "atr14" in frame.columns else 0.0
    zone_width = max(atr * 0.35, level * 0.0015)
    if zone_width <= 0.0:
        return 0.0
    touches = 0
    tail_df = frame.tail(24)
    for low, high in zip(tail_df["low"], tail_df["high"], strict=False):
        low_f = float(low or 0.0)
        high_f = float(high or 0.0)
        if low_f <= level + zone_width and high_f >= level - zone_width:
            touches += 1
    touch_score = min(touches / 4.0, 1.0)
    zone_width_pct = zone_width / level
    width_score = max(0.0, min(1.0 - (zone_width_pct / 0.02), 1.0))
    return max(0.0, min(touch_score * width_score, 1.0))


def _oi_momentum(prepared: PreparedSymbol, signal: Signal) -> float:
    """Score based on OI change + CVD proxy + basis (contango/backwardation).

    OI rising + CVD aligned with direction → high score (LONG_IN / SHORT_IN pattern).
    OI falling or CVD opposing direction → low score.
    Basis component: extreme contango penalises longs; backwardation penalises shorts.
    """
    # --- OI component ---
    oi_chg = prepared.oi_change_pct
    if oi_chg is None:
        oi_score = 0.5  # OI unavailable: neutral, continue to CVD/basis sub-components
    elif oi_chg >= 0.10:
        oi_score = 1.0
    elif oi_chg >= 0.05:
        oi_score = 0.75
    elif oi_chg >= 0.02:
        oi_score = 0.6
    elif oi_chg >= 0.0:
        oi_score = 0.5
    elif oi_chg >= -0.05:
        oi_score = 0.3
    else:
        oi_score = 0.1

    # --- CVD proxy component (delta_ratio from 15m candles) ---
    cvd_score = 0.5
    delta_source = signal.orderflow_delta_ratio
    if (
        delta_source is None
        and not prepared.work_15m.is_empty()
        and "delta_ratio" in prepared.work_15m.columns
    ):
        delta_source = prepared.work_15m.item(-1, "delta_ratio")
    if delta_source is not None:
        delta = max(0.0, min(float(delta_source or 0.5), 1.0))
        # For LONG: buying pressure (delta_ratio > 0.5) is bullish
        # For SHORT: selling pressure (delta_ratio < 0.5) is bullish
        if signal.direction == "long":
            cvd_score = max(0.0, min((delta - 0.3) / 0.4, 1.0))
        else:
            cvd_score = max(0.0, min((0.7 - delta) / 0.4, 1.0))

    # --- Basis component (contango vs backwardation) ---
    # Extreme contango (> +0.10%) = futures richly priced → crowd is bullish/crowded
    #   → headwind for LONG, tailwind for SHORT
    # Deep backwardation (< -0.05%) = futures cheap → forced selling / capitulation
    #   → tailwind for LONG reversal, neutral for SHORT
    basis = getattr(prepared, "basis_pct", None)
    if basis is None:
        basis_score = 0.5
    else:
        if signal.direction == "long":
            if basis <= -0.05:
                basis_score = 0.85  # backwardation = capitulation, good for long reversal
            elif basis <= 0.05:
                basis_score = 0.5
            elif basis <= 0.15:
                basis_score = 0.35  # mild contango = crowded longs
            else:
                basis_score = 0.15  # extreme contango = very crowded
        else:
            if basis >= 0.10:
                basis_score = 0.85  # high contango = crowded longs, good for short
            elif basis >= 0.03:
                basis_score = 0.65
            elif basis >= -0.03:
                basis_score = 0.5
            else:
                basis_score = 0.35  # backwardation = capitulation already done, risky short

    return round((oi_score * 0.55 + cvd_score * 0.35 + basis_score * 0.10), 4)


def _risk_reward_quality(signal: Signal, settings: BotSettings) -> float:
    stop_distance = float(getattr(signal, "stop_distance_pct", 0.0) or 0.0)
    if stop_distance <= 0.0:
        return 0.0  # stop distance pct: non-positive stop is invalid for R/R scoring.
    rr = float(signal.risk_reward or 0.0)
    filters = getattr(settings, "filters", None)
    setup_overrides = getattr(filters, "setups", {}) if filters is not None else {}
    if not isinstance(setup_overrides, dict):
        setup_overrides = {}
    min_risk_reward = (
        float(getattr(filters, "min_risk_reward", 1.9)) if filters is not None else 1.9
    )
    setup_params = setup_overrides.get(signal.setup_id, {})
    if not isinstance(setup_params, dict):
        setup_params = {}
    min_rr = float(setup_params.get("min_rr", min_risk_reward))
    max_rr = max(min_rr + 0.1, 4.0)
    if rr <= 0.0:
        return 0.0
    if rr < min_rr:
        return max(0.05, min((rr / max(min_rr, 1e-9)) * 0.45, 0.45))
    excess = (rr - min_rr) / max(max_rr - min_rr, 1e-9)
    return max(0.55, min(0.55 + excess * 0.45, 1.0))


def _funding_contrarian(prepared: PreparedSymbol, signal: Signal, settings: BotSettings) -> float:
    """Contrarian funding score.

    Extreme funding against direction → crowding opportunity (higher score).
    """
    funding = prepared.funding_rate
    if funding is None:
        return 0.5
    scoring_cfg = getattr(settings, "scoring", None)
    extreme = getattr(scoring_cfg, "funding_rate_extreme", None)
    moderate = getattr(scoring_cfg, "funding_rate_moderate", None)
    if extreme is None or moderate is None:
        if not _FUNDING_DEFAULT_WARNING_STATE["emitted"]:
            LOG.warning(
                "funding scoring thresholds missing; using defaults extreme=%.6f moderate=%.6f",
                _DEFAULT_FUNDING_RATE_EXTREME,
                _DEFAULT_FUNDING_RATE_MODERATE,
            )
            _FUNDING_DEFAULT_WARNING_STATE["emitted"] = True
        extreme = _DEFAULT_FUNDING_RATE_EXTREME if extreme is None else extreme
        moderate = _DEFAULT_FUNDING_RATE_MODERATE if moderate is None else moderate
    extreme = max(float(extreme), 1e-9)
    moderate = max(float(moderate), 1e-9)
    if signal.direction == "long":
        if funding <= -extreme:
            return 1.0
        if funding <= -moderate:
            return 0.75
        if funding >= extreme:
            return 0.0
        if funding >= moderate:
            return 0.25
        return 0.5
    if funding >= extreme:
        return 1.0
    if funding >= moderate:
        return 0.75
    if funding <= -extreme:
        return 0.0
    if funding <= -moderate:
        return 0.25
    return 0.5


def _crowding_context_stale(prepared: PreparedSymbol) -> bool:
    flags = getattr(prepared, "data_freshness_flags", ()) or ()
    return "crowding_context_missing" in flags


def _ratio_score(ratio: float | None, direction: str, *, contrarian: bool) -> float:
    if ratio is None or not math.isfinite(ratio) or ratio <= 0.0:
        return 0.5
    if contrarian:
        if direction == "long":
            if ratio <= 0.7:
                return 0.95
            if ratio <= 0.9:
                return 0.75
            if ratio >= 1.8:
                return 0.12
            if ratio >= 1.45:
                return 0.28
            if ratio >= 1.1:
                return 0.4
            return 0.5
        if ratio >= 1.35:
            return 0.95
        if ratio >= 1.1:
            return 0.75
        if ratio <= 0.55:
            return 0.12
        if ratio <= 0.8:
            return 0.28
        if ratio <= 0.92:
            return 0.4
        return 0.5
    if direction == "long":
        if 1.02 <= ratio <= 1.35:
            return 0.88
        if 0.94 <= ratio < 1.02:
            return 0.62
        if 1.35 < ratio <= 1.65:
            return 0.55
        if ratio > 1.65:
            return 0.2
        if ratio < 0.8:
            return 0.12
        return 0.35
    if 0.7 <= ratio <= 0.98:
        return 0.88
    if 0.98 < ratio <= 1.06:
        return 0.62
    if 0.5 <= ratio < 0.7:
        return 0.55
    if ratio < 0.5:
        return 0.2
    if ratio > 1.25:
        return 0.12
    return 0.35


def _gap_score(gap: float | None, direction: str, *, contrarian: bool) -> float:
    if gap is None or not math.isfinite(gap):
        return 0.5
    if contrarian:
        if direction == "long":
            if gap <= -0.12:
                return 0.9
            if gap <= -0.05:
                return 0.72
            if gap >= 0.22:
                return 0.15
            return 0.5
        if gap >= 0.12:
            return 0.9
        if gap >= 0.05:
            return 0.72
        if gap <= -0.22:
            return 0.15
        return 0.5
    if direction == "long":
        if 0.0 <= gap <= 0.18:
            return 0.82
        if gap < -0.08 or gap >= 0.3:
            return 0.18
        return 0.5
    if -0.18 <= gap <= 0.0:
        return 0.82
    if gap > 0.08 or gap <= -0.3:
        return 0.18
    return 0.5


def _crowd_position(prepared: PreparedSymbol, signal: Signal, _settings: BotSettings) -> float:
    contrarian_mode = (
        signal.strategy_family == "reversal"
        or signal.confirmation_profile == "countertrend_exhaustion"
    )
    ratio_scores = []
    if not _crowding_context_stale(prepared):
        ratio_scores.append(
            (
                _ratio_score(
                    prepared.top_account_ls_ratio or prepared.ls_ratio,
                    signal.direction,
                    contrarian=contrarian_mode,
                ),
                0.28,
            )
        )
        ratio_scores.append(
            (
                _ratio_score(
                    prepared.top_position_ls_ratio,
                    signal.direction,
                    contrarian=contrarian_mode,
                ),
                0.30,
            )
        )
        ratio_scores.append(
            (
                _ratio_score(
                    prepared.global_account_ls_ratio or prepared.global_ls_ratio,
                    signal.direction,
                    contrarian=contrarian_mode,
                ),
                0.20,
            )
        )
        ratio_scores.append(
            (
                _gap_score(
                    prepared.top_vs_global_ls_gap,
                    signal.direction,
                    contrarian=contrarian_mode,
                ),
                0.12,
            )
        )

    taker = getattr(prepared, "taker_ratio", None)
    if taker is None:
        taker_score = 0.5
    else:
        if signal.direction == "long":
            # Takers net buying → confirms long direction
            if taker >= 1.5:
                taker_score = 0.9
            elif taker >= 1.3:
                taker_score = 0.7
            elif taker <= 0.7:
                taker_score = 0.2  # takers selling → headwind for long
            elif taker <= 0.85:
                taker_score = 0.35
            else:
                taker_score = 0.5
        else:
            # Takers net selling → confirms short direction
            if taker <= 0.67:
                taker_score = 0.9
            elif taker <= 0.77:
                taker_score = 0.7
            elif taker >= 1.43:
                taker_score = 0.2  # takers buying → headwind for short
            elif taker >= 1.18:
                taker_score = 0.35
            else:
                taker_score = 0.5
    ratio_scores.append((taker_score, 0.10 if contrarian_mode else 0.20))

    weighted_total = 0.0
    total_weight = 0.0
    for score, weight in ratio_scores:
        weighted_total += score * weight
        total_weight += weight
    if total_weight <= 0.0:
        return 0.5
    return round(weighted_total / total_weight, 4)


def _liquidation_cluster_score(prepared: PreparedSymbol, signal: Signal) -> float:
    cascade = getattr(prepared, "liquidation_cascade_5m", None)
    liq_score = getattr(prepared, "liquidation_score", None)
    if cascade is not True:
        return 0.5
    if liq_score is None:
        return 0.65
    signed = float(liq_score) * (1.0 if signal.direction == "long" else -1.0)
    if signed > 0.25:
        return 0.9
    if signed < -0.25:
        return 0.15
    return 0.5


def _macd_alignment(prepared: PreparedSymbol, signal: Signal) -> float:
    frame = getattr(prepared, "work_15m", None)
    if frame is None or frame.is_empty():
        return 0.5
    if not all(c in frame.columns for c in ("macd_line", "macd_signal", "macd_hist")):
        return 0.5
    try:
        macd_line = float(frame.item(-1, "macd_line") or 0.0)
        macd_signal = float(frame.item(-1, "macd_signal") or 0.0)
        macd_hist = float(frame.item(-1, "macd_hist") or 0.0)
    except (IndexError, TypeError, ValueError):
        return 0.5
    if not (math.isfinite(macd_line) and math.isfinite(macd_signal) and math.isfinite(macd_hist)):
        return 0.5
    line_above_signal = macd_line > macd_signal
    hist_positive = macd_hist > 0.0
    if signal.direction == "long":
        if line_above_signal and hist_positive:
            return 0.90
        if line_above_signal or hist_positive:
            return 0.65
        if not line_above_signal and not hist_positive:
            return 0.25
        return 0.40
    if not line_above_signal and not hist_positive:
        return 0.90
    if not line_above_signal or not hist_positive:
        return 0.65
    if line_above_signal and hist_positive:
        return 0.25
    return 0.40


def _obv_alignment(prepared: PreparedSymbol, signal: Signal) -> float:
    frame = getattr(prepared, "work_15m", None)
    if frame is None or frame.is_empty():
        return 0.5
    if not all(c in frame.columns for c in ("obv", "obv_ema20", "obv_above_ema")):
        return 0.5
    try:
        obv_val = float(frame.item(-1, "obv") or 0.0)
        obv_ema = float(frame.item(-1, "obv_ema20") or 0.0)
        obv_above = float(frame.item(-1, "obv_above_ema") or 0.0)
    except (IndexError, TypeError, ValueError):
        return 0.5
    if not (math.isfinite(obv_val) and math.isfinite(obv_ema)):
        return 0.5
    obv_confirms = obv_above > 0.5
    if obv_val == 0.0 and obv_ema == 0.0:
        return 0.5
    obv_rising = obv_val > obv_ema
    if signal.direction == "long":
        if obv_confirms and obv_rising:
            return 0.85
        if obv_confirms or obv_rising:
            return 0.65
        return 0.30
    if not obv_confirms and not obv_rising:
        return 0.85
    if not obv_confirms or not obv_rising:
        return 0.65
    return 0.30


def _adx_strength(prepared: PreparedSymbol, signal: Signal) -> float:
    frame = getattr(prepared, "work_15m", None)
    if frame is None or frame.is_empty():
        return 0.5
    if not all(c in frame.columns for c in ("adx14", "plus_di14", "minus_di14")):
        return 0.5
    try:
        adx = float(frame.item(-1, "adx14") or 0.0)
        plus_di = float(frame.item(-1, "plus_di14") or 0.0)
        minus_di = float(frame.item(-1, "minus_di14") or 0.0)
    except (IndexError, TypeError, ValueError):
        return 0.5
    if not (math.isfinite(adx) and math.isfinite(plus_di) and math.isfinite(minus_di)):
        return 0.5
    if adx < 20.0:
        base = 0.15
    elif adx < 25.0:
        base = 0.45
    elif adx < 40.0:
        base = 0.70
    else:
        base = 0.90
    di_aligned = plus_di > minus_di if signal.direction == "long" else minus_di > plus_di
    if di_aligned:
        return min(base + 0.10, 1.0)
    return max(base - 0.10, 0.0)


def _keltner_position(prepared: PreparedSymbol, signal: Signal) -> float:
    frame = getattr(prepared, "work_15m", None)
    if frame is None or frame.is_empty():
        return 0.5
    if not all(c in frame.columns for c in ("kc_upper", "kc_lower", "close")):
        return 0.5
    try:
        close = float(frame.item(-1, "close") or 0.0)
        upper = float(frame.item(-1, "kc_upper") or 0.0)
        lower = float(frame.item(-1, "kc_lower") or 0.0)
    except (IndexError, TypeError, ValueError):
        return 0.5
    if not (math.isfinite(close) and math.isfinite(upper) and math.isfinite(lower)):
        return 0.5
    span = upper - lower
    if span <= 0.0:
        return 0.5
    position = (close - lower) / span
    if signal.direction == "long":
        if position <= 0.15:
            return 0.85
        if position <= 0.40:
            return 0.70
        if position >= 0.85:
            return 0.20
        if position >= 0.60:
            return 0.40
        return 0.55
    if position >= 0.85:
        return 0.85
    if position >= 0.60:
        return 0.70
    if position <= 0.15:
        return 0.20
    if position <= 0.40:
        return 0.40
    return 0.55


def _vwap_position(prepared: PreparedSymbol, signal: Signal) -> float:
    frame = getattr(prepared, "work_15m", None)
    if frame is None or frame.is_empty():
        return 0.5
    if "vwap_deviation_atr14" in frame.columns:
        col = "vwap_deviation_atr14"
    elif "vwap_deviation_pct" in frame.columns:
        col = "vwap_deviation_pct"
    else:
        return 0.5
    try:
        deviation = float(frame.item(-1, col) or 0.0)
    except (IndexError, TypeError, ValueError):
        return 0.5
    if not math.isfinite(deviation):
        return 0.5
    if signal.direction == "long":
        if deviation > 0.5:
            return 0.15
        if deviation > 0.15:
            return 0.35
        if deviation < -0.5:
            return 0.90
        if deviation < -0.15:
            return 0.75
        if deviation < -0.05:
            return 0.60
        return 0.50
    if deviation < -0.5:
        return 0.15
    if deviation < -0.15:
        return 0.35
    if deviation > 0.5:
        return 0.90
    if deviation > 0.15:
        return 0.75
    if deviation > 0.05:
        return 0.60
    return 0.50


def _regime_alignment_bonus(prepared: PreparedSymbol, signal: Signal) -> float:
    score_4h = _directional_alignment(prepared.regime_4h_confirmed, signal.direction)
    if prepared.primary_timeframe != "15m":
        primary = _structure_frame(prepared)
        score_primary = _directional_alignment(_regime_confirmed(primary), signal.direction)
    else:
        score_primary = _directional_alignment(prepared.structure_1h, signal.direction)
    combined = score_primary * 0.60 + score_4h * 0.40
    if combined >= 0.8:
        return 0.90
    if combined >= 0.5:
        return 0.60
    if combined >= 0.2:
        return 0.40
    return 0.15


def _volume_profile_position(prepared: PreparedSymbol, signal: Signal) -> float:
    vah = prepared.vah_15m or prepared.vah_1h
    val = prepared.val_15m or prepared.val_1h
    if vah is None or val is None or vah <= val:
        return 0.5
    frame = getattr(prepared, "work_15m", None)
    if frame is None or frame.is_empty() or "close" not in frame.columns:
        return 0.5
    try:
        close = float(frame.item(-1, "close") or 0.0)
    except (IndexError, TypeError, ValueError):
        return 0.5
    if not math.isfinite(close):
        return 0.5
    if signal.direction == "long":
        if close <= val:
            return 0.80
        if close <= val * 1.02:
            return 0.65
        if close >= vah:
            return 0.25
        return 0.50
    if close >= vah:
        return 0.80
    if close >= vah * 0.98:
        return 0.65
    if close <= val:
        return 0.25
    return 0.50


def _pivot_proximity(prepared: PreparedSymbol, signal: Signal) -> float:
    frame = getattr(prepared, "work_15m", None)
    if frame is None or frame.is_empty() or not all(c in frame.columns for c in ("high", "low")):
        return 0.5
    try:
        close = float(frame.item(-1, "close"))
    except (IndexError, TypeError, ValueError):
        return 0.5
    if not math.isfinite(close):
        return 0.5
    sh_mask, sl_mask = _swing_points(frame, n=3)
    swing_highs = frame.filter(sh_mask)["high"].tail(3).to_list()
    swing_lows = frame.filter(sl_mask)["low"].tail(3).to_list()
    if signal.direction == "long":
        if not swing_lows:
            return 0.5
        nearest_pivot = min(swing_lows, key=lambda p: abs(float(p) - close))
        dist = abs(float(nearest_pivot) - close) / max(close, 1.0)
        if dist < 0.002:
            return 0.85
        if dist < 0.005:
            return 0.70
        return 0.50
    if not swing_highs:
        return 0.5
    nearest_pivot = min(swing_highs, key=lambda p: abs(float(p) - close))
    dist = abs(float(nearest_pivot) - close) / max(close, 1.0)
    if dist < 0.002:
        return 0.85
    if dist < 0.005:
        return 0.70
    return 0.50


def _btc_correlation_penalty(prepared: PreparedSymbol, signal: Signal) -> float:
    btc_change = getattr(prepared, "btc_change_pct", None)
    eth_change = getattr(prepared, "eth_change_pct", None)
    ref_change: float | None = btc_change if btc_change is not None else eth_change
    if ref_change is None:
        return 0.5
    try:
        ref_change = float(ref_change)
    except (TypeError, ValueError):
        return 0.5
    if not math.isfinite(ref_change):
        return 0.5
    if signal.direction == "long":
        if ref_change < -2.0:
            return 0.10
        if ref_change < -1.0:
            return 0.30
        if ref_change < -0.5:
            return 0.45
        if ref_change > 2.0:
            return 0.85
        if ref_change > 1.0:
            return 0.70
        return 0.55
    if ref_change > 2.0:
        return 0.10
    if ref_change > 1.0:
        return 0.30
    if ref_change > 0.5:
        return 0.45
    if ref_change < -2.0:
        return 0.85
    if ref_change < -1.0:
        return 0.70
    return 0.55


def _session_killzone_score(signal: Signal) -> float:
    from datetime import UTC, datetime  # noqa: PLC0415

    hour = datetime.now(UTC).hour
    in_killzone = hour in {0, 1, 2, 7, 8, 9, 12, 13, 14}
    if not in_killzone:
        return 0.30
    if signal.strategy_family in {"session", "breakout", "momentum"}:
        return 0.78
    return 0.58

"""structure_pullback — canonical strategy detector."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..features import _swing_points as _sp
from ..setups import _build_signal, _compute_dynamic_score, _pullback_levels, _reject
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ..setups.utils import select_structural_target
from ._common import SpecHit, _clean_impulse, _latest_values, as_float, with_spec_columns

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_structure_pullback"]


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
        vol_ratio = current.get("volume_ratio20", 1.0)
        if vol_ratio < 0.85:
            return None
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
        vol_ratio = current.get("volume_ratio20", 1.0)
        if vol_ratio < 0.85:
            return None
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


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _detect_structure_pullback_extended(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    _setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    work_4h = prepared.work_4h
    work_1h = prepared.work_1h
    work_15m = prepared.work_15m
    # FIX 2026-05-21: spec pullback only accepts a narrow fib window; on a
    # miss, continue into the configured trend/pullback-level detector.
    min_trend_score = dynamic_params.get("min_trend_score", defaults["min_trend_score"])
    pullback_lookback = int(dynamic_params.get("pullback_lookback", defaults["pullback_lookback"]))
    sl_buffer_atr = dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"])
    min_rr = dynamic_params.get("min_rr", defaults["min_rr"])
    ema_proximity_cfg = float(
        dynamic_params.get("ema_proximity_pct", defaults["ema_proximity_pct"])
    )
    ema_deep_pullback_cfg = float(
        dynamic_params.get("ema_deep_pullback_pct", defaults["ema_deep_pullback_pct"])
    )
    ema_proximity_tol = (
        max(0.0, 1.0 - ema_proximity_cfg)
        if ema_proximity_cfg > 0.5
        else max(0.0, ema_proximity_cfg)
    )
    ema_deep_pullback_tol = (
        max(ema_proximity_tol, 1.0 - ema_deep_pullback_cfg)
        if ema_deep_pullback_cfg > 0.5
        else max(ema_proximity_tol, ema_deep_pullback_cfg)
    )

    if work_1h.height < 5 or work_15m.height < 5:
        _reject(prepared, "structure_pullback", "insufficient_bars")
        return None

    # Use 1H context for 15M signals (not 4H - too lagging for <4h trades)
    regime_1h = prepared.regime_1h_confirmed
    regime_4h = prepared.regime_4h_confirmed
    bias_1h = prepared.bias_1h
    # Use live 15m momentum to resolve neutral 1h bias for fresh pullbacks.
    if bias_1h == "neutral":
        roc10 = _as_float(work_15m.item(-1, "roc10")) if "roc10" in work_15m.columns else 0.0
        if roc10 > 0.20:
            bias_1h = "uptrend"
        elif roc10 < -0.20:
            bias_1h = "downtrend"
    structure = prepared.structure_1h
    penalty_multiplier = 1.0
    penalty_reasons: list[str] = []

    if (
        regime_1h in {"uptrend", "downtrend"}
        and structure in {"uptrend", "downtrend"}
        and structure != regime_1h
    ):
        penalty_multiplier *= float(dynamic_params.get("structure_regime_conflict_penalty", 0.84))
        penalty_reasons.append(f"structure_regime_conflict={structure}/{regime_1h}")
    ema20_1h = _as_float(work_1h.item(-1, "ema20"))
    close_1h = _as_float(work_1h.item(-1, "close"))
    if ema20_1h <= 0.0:
        _reject(prepared, "structure_pullback", "ema20_invalid", ema20_1h=ema20_1h)
        return None

    long_score = 0.0
    long_reasons: list[str] = []
    if regime_1h == "uptrend":
        long_score += 0.40
        long_reasons.append("1h_uptrend")
    elif bias_1h == "uptrend":
        long_score += 0.25
        long_reasons.append("1h_bias_uptrend")
    if structure == "uptrend":
        long_score += 0.30
        long_reasons.append("1h_structure_uptrend")
    elif structure == "ranging" and regime_1h == "uptrend":
        long_score += 0.15
        long_reasons.append("1h_ranging_in_1h_uptrend")
    ema20_proximity = abs(close_1h - ema20_1h) / ema20_1h
    if close_1h > ema20_1h:
        long_score += 0.20
        long_reasons.append("price_above_ema20")
    elif ema20_proximity <= ema_proximity_tol:
        long_score += 0.15
        long_reasons.append("price_near_ema20")
    elif ema20_proximity <= ema_deep_pullback_tol:
        long_score += 0.05
        long_reasons.append("deep_pullback_near_ema20")

    short_score = 0.0
    short_reasons: list[str] = []
    if regime_1h == "downtrend":
        short_score += 0.40
        short_reasons.append("1h_downtrend")
    elif bias_1h == "downtrend":
        short_score += 0.25
        short_reasons.append("1h_bias_downtrend")
    if structure == "downtrend":
        short_score += 0.30
        short_reasons.append("1h_structure_downtrend")
    elif structure == "ranging" and regime_1h == "downtrend":
        short_score += 0.15
        short_reasons.append("1h_ranging_in_1h_downtrend")
    if close_1h < ema20_1h:
        short_score += 0.20
        short_reasons.append("price_below_ema20")
    elif ema20_proximity <= ema_proximity_tol:
        short_score += 0.15
        short_reasons.append("price_near_ema20")
    elif ema20_proximity <= ema_deep_pullback_tol:
        short_score += 0.05
        short_reasons.append("deep_pullback_near_ema20")

    if (
        max(long_score, short_score) < float(min_trend_score)
        or abs(long_score - short_score) < 0.03
    ):
        _reject(
            prepared,
            "structure_pullback",
            "trend_score_too_low",
            long_score=round(long_score, 3),
            short_score=round(short_score, 3),
            regime=regime_1h,
            structure=structure,
            close=round(close_1h, 4),
            ema20=round(float(ema20_1h), 4),
        )
        return None

    if long_score > short_score:
        direction = "long"
        trend_reasons = long_reasons
    else:
        direction = "short"
        trend_reasons = short_reasons

    if "adx14" not in work_1h.columns:
        _reject(prepared, "structure_pullback", "missing_column_adx14")
        return None
    adx_1h = _as_float(work_1h.item(-1, "adx14"))
    # Use per-setup min_adx from config if available, else fallback to global
    _filters = getattr(settings, "filters", None)
    global_min_adx = getattr(_filters, "min_adx_1h", 15.0)
    min_adx = effective.get("min_adx_1h", defaults.get("min_adx_1h", global_min_adx))
    if adx_1h > 0.0 and adx_1h < min_adx:
        _reject(
            prepared,
            "structure_pullback",
            "adx_too_low_1h",
            adx_1h=round(adx_1h, 2),
            min_adx=min_adx,
        )
        return None

    prev_low = _as_float(work_15m.item(-2, "low"))
    prev_high = _as_float(work_15m.item(-2, "high"))
    trig_close = _as_float(work_15m.item(-1, "close"))
    atr = _as_float(work_15m.item(-1, "atr14"))
    if atr <= 0.0:
        _reject(prepared, "structure_pullback", "atr_non_positive", atr=atr)
        return None

    selected_level_name: str | None = None
    selected_level: float | None = None
    # loosened for high-beta alts where atr*0.20 < tick noise
    touch_tolerance = max(atr * 0.35, trig_close * 0.002)
    recent_pullback = work_15m.tail(int(max(2, pullback_lookback)))
    for level_name, level in _pullback_levels(prepared, direction):
        if level <= 0.0:
            continue
        if direction == "long":
            local_low = _as_float(recent_pullback["low"].min(), prev_low)
            touched = min(prev_low, local_low) <= level + touch_tolerance
            bounced = trig_close > level
        else:
            local_high = _as_float(recent_pullback["high"].max(), prev_high)
            touched = max(prev_high, local_high) >= level - touch_tolerance
            bounced = trig_close < level
        if (
            touched
            and bounced
            and (
                selected_level is None or abs(trig_close - level) < abs(trig_close - selected_level)
            )
        ):
            selected_level_name = level_name
            selected_level = level

    if selected_level is None or selected_level_name is None:
        _reject(prepared, "structure_pullback", "no_valid_pullback_level")
        return None

    level = selected_level
    vol_ratio = _as_float(work_15m.item(-1, "volume_ratio20"), 1.0)
    min_volume_ratio = float(
        dynamic_params.get("min_volume_ratio", defaults.get("min_volume_ratio", 0.8))
    )
    if vol_ratio < min_volume_ratio:
        _reject(prepared, "structure_pullback", "volume_too_low", vol_ratio=vol_ratio)
        return None

    rsi = _as_float(work_15m.item(-1, "rsi14"), 50.0)
    rsi_long_min = float(dynamic_params.get("rsi_long_min", defaults.get("rsi_long_min", 25.0)))
    rsi_long_max = float(dynamic_params.get("rsi_long_max", defaults.get("rsi_long_max", 80.0)))
    rsi_short_min = float(dynamic_params.get("rsi_short_min", defaults.get("rsi_short_min", 20.0)))
    rsi_short_max = float(dynamic_params.get("rsi_short_max", defaults.get("rsi_short_max", 75.0)))
    if direction == "long" and not (rsi_long_min <= rsi <= rsi_long_max):
        _reject(
            prepared,
            "structure_pullback",
            "rsi_out_of_range",
            direction=direction,
            rsi=rsi,
        )
        return None
    if direction == "short" and not (rsi_short_min <= rsi <= rsi_short_max):
        _reject(
            prepared,
            "structure_pullback",
            "rsi_out_of_range",
            direction=direction,
            rsi=rsi,
        )
        return None

    bb_pct_b = work_15m.item(-1, "bb_pct_b")
    if bb_pct_b is not None:
        try:
            bb_pct_b = float(bb_pct_b)
        except (TypeError, ValueError):
            bb_pct_b = None
    if bb_pct_b is not None:
        if direction == "long" and bb_pct_b > 0.90:
            penalty_multiplier *= float(dynamic_params.get("bb_extreme_penalty", 0.88))
            penalty_reasons.append(f"bb_extreme_long={bb_pct_b:.3f}")
        if direction == "short" and bb_pct_b < 0.10:
            penalty_multiplier *= float(dynamic_params.get("bb_extreme_penalty", 0.88))
            penalty_reasons.append(f"bb_extreme_short={bb_pct_b:.3f}")

    reasons = [
        f"1h regime_confirmed={regime_1h}",
        f"1h structure={structure}",
        *trend_reasons,
        f"pullback to {selected_level_name}={level:.4f}",
        f"vol_ratio={vol_ratio:.2f}",
        f"rsi={rsi:.1f}",
        *penalty_reasons,
    ]
    if (
        regime_1h in {"uptrend", "downtrend"}
        and regime_4h in {"uptrend", "downtrend"}
        and regime_1h != regime_4h
    ):
        reasons.append(f"macro_4h_conflict={regime_4h}")

    price_anchor = level
    reasons.append(f"limit_entry={price_anchor:.4f}")

    # --- Compute structural SL/TP ---
    work_15m_tail = work_15m.tail(10)
    _sh15, _sl15 = _sp(work_15m_tail, n=2)
    sh_1h_mask, sl_1h_mask = _sp(work_1h, n=3, include_unconfirmed_tail=True)
    sh_4h_mask = None
    sl_4h_mask = None
    if work_4h is not None and not work_4h.is_empty():
        sh_4h_mask, sl_4h_mask = _sp(work_4h, n=2)

    if direction == "long":
        # SL: below pullback swing low (last 3-5 15m bars) + 0.15xATR noise buffer
        last_10_lows = work_15m_tail["low"]
        sl_candidates = last_10_lows.filter(_sl15) if _sl15 is not None else last_10_lows
        fallback_low = work_15m.tail(5)["low"].min()
        pullback_low = (
            _as_float(sl_candidates.min()) if sl_candidates.len() > 0 else _as_float(fallback_low)
        )
        stop = pullback_low - atr * float(sl_buffer_atr)

        # TP1: prior 1h swing high above entry (trend extreme before pullback)
        tp1 = select_structural_target(
            work_1h,
            mask=sh_1h_mask,
            column="high",
            price_anchor=price_anchor,
            direction="long",
        )

        # TP2: next 4h swing high beyond TP1
        if work_4h is not None and not work_4h.is_empty() and sh_4h_mask is not None:
            tp2 = select_structural_target(
                work_4h,
                mask=sh_4h_mask,
                column="high",
                price_anchor=price_anchor,
                direction="long",
            )
        else:
            tp2 = None
    else:
        # SL: above pullback swing high + 0.4xATR noise buffer
        last_10_highs = work_15m_tail["high"]
        sh_candidates = last_10_highs.filter(_sh15) if _sh15 is not None else last_10_highs
        fallback_high = work_15m.tail(5)["high"].max()
        pullback_high = (
            _as_float(sh_candidates.max()) if sh_candidates.len() > 0 else _as_float(fallback_high)
        )
        stop = pullback_high + atr * float(sl_buffer_atr)

        # TP1: prior 1h swing low below entry
        tp1 = select_structural_target(
            work_1h,
            mask=sl_1h_mask,
            column="low",
            price_anchor=price_anchor,
            direction="short",
        )

        # TP2: next 4h swing low beyond TP1
        if work_4h is not None and not work_4h.is_empty() and sl_4h_mask is not None:
            tp2 = select_structural_target(
                work_4h,
                mask=sl_4h_mask,
                column="low",
                price_anchor=price_anchor,
                direction="short",
            )
        else:
            tp2 = None

    # Validate: TP1 must be at least 1.5x risk distance, else reject setup
    risk = abs(price_anchor - stop)
    if risk <= 0:
        _reject(prepared, "structure_pullback", "invalid_stop", stop=stop)
        return None
    min_rr_cfg = float(min_rr)
    min_required = risk * min_rr_cfg
    if tp1 is None or abs(tp1 - price_anchor) < min_required:
        tp1 = price_anchor + min_required if direction == "long" else price_anchor - min_required
        reasons.append("tp1_rr_fallback")
    if tp2 is None or abs(tp2 - price_anchor) <= abs(tp1 - price_anchor):
        tp2 = (
            price_anchor + risk * max(2.0, min_rr_cfg + 0.35)
            if direction == "long"
            else price_anchor - risk * max(2.0, min_rr_cfg + 0.35)
        )

    score = _compute_dynamic_score(
        direction=direction,
        base_score=float(dynamic_params.get("base_score", defaults["base_score"])),
        vol_ratio=vol_ratio,
        rsi=rsi,
        structure_clarity=0.6,
    )
    score *= penalty_multiplier

    return _build_signal(
        prepared=prepared,
        setup_id="structure_pullback",
        direction=direction,
        score=score,
        timeframe="15m+1h",
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=price_anchor,
        atr=atr,
    )


def detect_structure_pullback_setup(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    spec_kwargs = None
    return run_setup_detection(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        defaults=defaults,
        effective=effective,
        spec_detect=detect_structure_pullback,
        extended_detect=_detect_structure_pullback_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = [
    "_detect_structure_pullback_extended",
    "detect_structure_pullback",
    "detect_structure_pullback_setup",
]


class StructurePullbackSetup(SpecDetectorSetup):
    setup_id = "structure_pullback"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.55,
        "bias_mismatch_penalty": 0.75,
        "tp_too_close_penalty": 0.75,
        "min_rr": 1.9,
        "min_trend_score": 0.4,
        "ema_proximity_pct": 0.99,
        "ema_deep_pullback_pct": 0.965,
        "pullback_lookback": 12.0,
        "sl_buffer_atr": 0.5,
        "min_adx_1h": 15.0,
        "min_volume_ratio": 0.8,
        "rsi_long_min": 25.0,
        "rsi_long_max": 80.0,
        "rsi_short_min": 20.0,
        "rsi_short_max": 75.0,
    }

    detect_setup = detect_structure_pullback_setup

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        """Tunable parameters for self-learner optimization."""
        defaults = {
            "base_score": 0.55,
            "bias_mismatch_penalty": 0.75,
            "tp_too_close_penalty": 0.75,
            "min_rr": 1.9,
            "min_trend_score": 0.40,
            "ema_proximity_pct": 0.990,
            "ema_deep_pullback_pct": 0.965,
            "pullback_lookback": 12.0,
            "sl_buffer_atr": 0.5,
            "min_adx_1h": 15.0,
        }
        if settings is not None:
            filters = getattr(settings, "filters", None)
            if filters:
                setups_config = getattr(filters, "setups", {})
                if isinstance(setups_config, dict) and self.setup_id in setups_config:
                    return {**defaults, **setups_config.get(self.setup_id, {})}
        return defaults


__all__ = ["StructurePullbackSetup"]

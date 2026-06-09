"""wick_trap_reversal - canonical strategy detector."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ..features.prepare import _swing_points
from ..setups import _build_signal, _compute_dynamic_score, _last_swing_prices, _reject
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ..setups.utils import normalize_trade_levels
from ._common import SpecHit, _latest_values, confirmed_pattern_frame, with_spec_columns

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_wick_trap"]


def detect_wick_trap(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    body = row.get("spec_body", 0.0)
    if atr <= 0.0 or body < atr * 0.25:
        return None
    prev_high = row.get("spec_prev_high20", 0.0)
    prev_low = row.get("spec_prev_low20", 0.0)
    vol_ratio = row.get("volume_ratio20", 1.0)
    wick_mult = 1.1
    lower_wick = row.get("spec_lower_wick_ratio", 0.0)
    upper_wick = row.get("spec_upper_wick_ratio", 0.0)
    if row["low"] < prev_low and lower_wick >= wick_mult and vol_ratio >= 0.85:
        return SpecHit(
            strategy="wick_trap_reversal",
            direction="long",
            entry=prev_low,
            stop_basis=row["low"],
            atr=atr,
            timeframe=timeframe,
            reasons=(f"new_low_wick_trap={prev_low:.4f}", f"body_atr={body / atr:.2f}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    if row["high"] > prev_high and upper_wick >= wick_mult and vol_ratio >= 0.85:
        return SpecHit(
            strategy="wick_trap_reversal",
            direction="short",
            entry=prev_high,
            stop_basis=row["high"],
            atr=atr,
            timeframe=timeframe,
            reasons=(f"new_high_wick_trap={prev_high:.4f}", f"body_atr={body / atr:.2f}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    return None


detect_wick_trap_reversal = detect_wick_trap


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _detect_wick_trap_reversal_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    work_1h = confirmed_pattern_frame(prepared.work_1h)
    work_15m = confirmed_pattern_frame(prepared.work_15m)

    if work_1h.height < 10 or work_15m.height < 8:
        _reject(prepared, setup_id, "insufficient_bars")
        return None

    atr = _as_float(work_15m.item(-1, "atr14"))
    if atr <= 0.0:
        _reject(prepared, setup_id, "atr_non_positive", atr=atr)
        return None
    # FIX 2026-05-21: spec trap is narrow; fall through to the confirmed
    # 1h swing sweep detector before declaring no wick trap.
    wick_through_atr_mult = float(
        dynamic_params.get(
            "wick_through_atr_mult",
            dynamic_params.get("wick_atr_threshold", defaults["wick_through_atr_mult"]),
        )
    )
    closed_back_threshold = max(
        float(dynamic_params.get("closed_back_threshold", atr * 0.1)),
        atr
        * float(
            dynamic_params.get(
                "closed_back_atr_mult",
                defaults["closed_back_atr_mult"],
            )
        ),
    )

    sh_mask, sl_mask = _swing_points(work_1h, n=3, include_unconfirmed_tail=True)

    direction: str | None = None
    wick_bar_idx: int | None = None
    level: float | None = None

    def _recent_15m_positions_after(event_time: object) -> list[int]:
        positions: list[int] = []
        start_idx = max(0, work_15m.height - 12)
        event_dt = _to_utc(event_time if isinstance(event_time, datetime) else None)
        for idx in range(start_idx, work_15m.height):
            bar_time = work_15m.item(idx, "time")
            bar_dt = _to_utc(bar_time if isinstance(bar_time, datetime) else None)
            if event_dt is not None and bar_dt is not None and bar_dt <= event_dt:
                continue
            positions.append(idx)
        return positions

    if sl_mask.any():
        # Get positions where sl_mask is True (swing low indices)
        sl_positions = [idx for idx, is_swing in enumerate(sl_mask.to_list()) if is_swing]
        for sl_pos in reversed(sl_positions):
            bars_ago = work_1h.height - 1 - sl_pos
            if 3 <= bars_ago <= 20:
                candidate_level = float(work_1h["low"][sl_pos])
                swing_time = work_1h.item(sl_pos, "time")
                for k in _recent_15m_positions_after(swing_time):
                    bar_low = float(work_15m.item(k, "low"))
                    bar_close = float(work_15m.item(k, "close"))
                    wick_through = bar_low < candidate_level - atr * wick_through_atr_mult
                    closed_back = bar_close > candidate_level + closed_back_threshold
                    if wick_through and closed_back:
                        direction = "long"
                        wick_bar_idx = k
                        level = candidate_level
                        break
                if direction is not None:
                    break

    if direction is None and sh_mask.any():
        # Get positions where sh_mask is True (swing high indices)
        sh_positions = [idx for idx, is_swing in enumerate(sh_mask.to_list()) if is_swing]
        for sh_pos in reversed(sh_positions):
            bars_ago = work_1h.height - 1 - sh_pos
            if 3 <= bars_ago <= 20:
                candidate_level = float(work_1h["high"][sh_pos])
                swing_time = work_1h.item(sh_pos, "time")
                for k in _recent_15m_positions_after(swing_time):
                    bar_high = float(work_15m.item(k, "high"))
                    bar_close = float(work_15m.item(k, "close"))
                    wick_through = bar_high > candidate_level + atr * wick_through_atr_mult
                    closed_back = bar_close < candidate_level - closed_back_threshold
                    if wick_through and closed_back:
                        direction = "short"
                        wick_bar_idx = k
                        level = candidate_level
                        break
                if direction is not None:
                    break

    if direction is None or level is None or wick_bar_idx is None:
        _reject(prepared, "wick_trap_reversal", "no_wick_trap_detected")
        return None

    confirmation_lag = work_15m.height - 1 - wick_bar_idx
    max_confirmation_bars = int(
        dynamic_params.get("max_confirmation_bars", defaults["max_confirmation_bars"])
    )
    if confirmation_lag > max_confirmation_bars:
        _reject(
            prepared,
            "wick_trap_reversal",
            "wick_confirmation_too_late",
            confirmation_lag=confirmation_lag,
            max_confirmation_bars=max_confirmation_bars,
        )
        return None

    trigger_vol_ratio = _as_float(work_15m.item(-1, "volume_ratio20"), 1.0)
    wick_vol_ratio = _as_float(work_15m.item(wick_bar_idx, "volume_ratio20"), 1.0)
    vol_ratio = max(trigger_vol_ratio, wick_vol_ratio)
    min_volume_ratio = float(dynamic_params.get("min_volume_ratio", defaults["min_volume_ratio"]))
    rsi = _as_float(work_15m.item(-1, "rsi14"), 50.0)
    st_15m = work_15m.item(-1, "supertrend_dir")
    if st_15m is not None:
        try:
            st_15m = float(st_15m)
        except TypeError, ValueError:
            st_15m = None
    trig_high = float(work_15m.item(-1, "high"))
    trig_low = float(work_15m.item(-1, "low"))
    trig_close = float(work_15m.item(-1, "close"))
    candle_range = max(trig_high - trig_low, 0.0)
    close_strength_ok = False
    if candle_range > 0.0:
        if direction == "long":
            close_strength_ok = ((trig_close - trig_low) / candle_range) > 0.7
        else:
            close_strength_ok = ((trig_high - trig_close) / candle_range) > 0.7

    supertrend_opposes = bool(
        st_15m is not None
        and ((direction == "long" and st_15m < 0) or (direction == "short" and st_15m > 0))
    )

    if direction == "long":
        if trig_close <= level:
            _reject(
                prepared,
                "wick_trap_reversal",
                "trigger_close_below_level",
                close=trig_close,
                level=level,
            )
            return None
        if vol_ratio < min_volume_ratio and not close_strength_ok:
            _reject(
                prepared,
                "wick_trap_reversal",
                "no_confirmation",
                wick_vol_ratio=wick_vol_ratio,
                trigger_vol_ratio=trigger_vol_ratio,
                min_volume_ratio=min_volume_ratio,
                close_strength_ok=close_strength_ok,
            )
            return None
    else:
        if trig_close >= level:
            _reject(
                prepared,
                "wick_trap_reversal",
                "trigger_close_above_level",
                close=trig_close,
                level=level,
            )
            return None
        if vol_ratio < min_volume_ratio and not close_strength_ok:
            _reject(
                prepared,
                "wick_trap_reversal",
                "no_confirmation",
                wick_vol_ratio=wick_vol_ratio,
                trigger_vol_ratio=trigger_vol_ratio,
                min_volume_ratio=min_volume_ratio,
                close_strength_ok=close_strength_ok,
            )
            return None

    wick_bar_close = float(work_15m.item(wick_bar_idx, "close"))
    reasons = [
        f"wick_sweep level={level:.4f}",
        f"direction={direction}",
        f"wick_bar close={wick_bar_close:.4f}",
        f"wick_vol_ratio={wick_vol_ratio:.2f}",
        f"trigger_vol_ratio={trigger_vol_ratio:.2f}",
        f"confirmation_lag={confirmation_lag}",
        f"close_strength_ok={close_strength_ok}",
        f"rsi={rsi:.1f}",
    ]
    if supertrend_opposes:
        reasons.append(f"supertrend_opposes_penalty={st_15m:.0f}")

    price_anchor = float(level)
    reasons.append(f"limit_entry={price_anchor:.4f}")

    sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]))

    # --- Compute structural SL/TP ---
    if direction == "long":
        # SL: beyond wick extreme (absolute tip of sweep wick) + configured ATR buffer.
        wick_bar_low = float(work_15m.item(wick_bar_idx, "low"))
        stop = wick_bar_low - atr * sl_buffer_atr
        last_sh, _ = _last_swing_prices(work_1h)
        tp1 = last_sh if (last_sh and last_sh > price_anchor) else None
        tp2 = None
    else:
        # SL: beyond wick extreme + configured ATR buffer.
        wick_bar_high = float(work_15m.item(wick_bar_idx, "high"))
        stop = wick_bar_high + atr * sl_buffer_atr
        _, last_sl = _last_swing_prices(work_1h)
        tp1 = last_sl if (last_sl and last_sl < price_anchor) else None
        tp2 = None

    # Validate runtime RR with a deterministic fallback target when structure is too close.
    risk = abs(price_anchor - stop)
    if risk <= 0:
        _reject(prepared, "wick_trap_reversal", "invalid_stop", stop=stop)
        return None
    min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
    min_required = risk * min_rr
    fallback_note = None
    if tp1 is None or abs(tp1 - price_anchor) < min_required:
        tp1 = price_anchor + min_required if direction == "long" else price_anchor - min_required
        fallback_note = f"tp1_rr_fallback_{min_rr:.2f}"
    if tp2 is None or abs(tp2 - price_anchor) <= abs(tp1 - price_anchor):
        tp2 = (
            price_anchor + risk * max(2.0, min_rr + 0.35)
            if direction == "long"
            else price_anchor - risk * max(2.0, min_rr + 0.35)
        )
    if fallback_note:
        reasons.append(fallback_note)
    normalized_levels = normalize_trade_levels(
        direction=direction,
        price_anchor=price_anchor,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
    )
    if normalized_levels is None:
        _reject(
            prepared,
            "wick_trap_reversal",
            "invalid_trade_levels",
            direction=direction,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=price_anchor,
        )
        return None
    stop, tp1, tp2, _, _ = normalized_levels

    score = _compute_dynamic_score(
        direction=direction,
        base_score=float(dynamic_params.get("base_score", defaults["base_score"])),
        vol_ratio=vol_ratio,
        rsi=rsi,
        structure_clarity=0.5,
    )
    if supertrend_opposes:
        score *= float(dynamic_params.get("supertrend_opposes_penalty", 0.88))

    return _build_signal(
        prepared=prepared,
        setup_id="wick_trap_reversal",
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


def detect_wick_trap_reversal_setup(
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
        spec_detect=detect_wick_trap,
        extended_detect=_detect_wick_trap_reversal_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = [
    "_detect_wick_trap_reversal_extended",
    "detect_wick_trap",
    "detect_wick_trap_reversal_setup",
]


class WickTrapReversalSetup(SpecDetectorSetup):
    setup_id = "wick_trap_reversal"
    ENTRY_ORDER_TYPE: ClassVar[str] = "limit"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.55,
        "bias_mismatch_penalty": 0.75,
        "tp_too_close_penalty": 0.75,
        "sl_buffer_atr": 0.8,
        "min_rr": 1.9,
        "wick_atr_threshold": 0.3,
        "wick_through_atr_mult": 0.3,
        "closed_back_atr_mult": 0.1,
        "min_volume_ratio": 1.2,
        "max_confirmation_bars": 8,
    }

    detect_setup = detect_wick_trap_reversal_setup


__all__ = ["WickTrapReversalSetup"]

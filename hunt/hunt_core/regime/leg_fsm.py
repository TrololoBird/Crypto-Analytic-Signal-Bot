"""Regime leg FSM — canonical lifecycle state machine (P4 migrated from detect/lifecycle).

VELVET short worked: fade at impulse *high* (RSI OB, rejection, cascade down).
BEAT short failed when lifecycle used rally_from_24h_low as "post_dump_bounce" on a
+89% up-leg (+400% multi-day) — that metric is the pump, not recovery after a dump.

Phases gate Telegram — not delivery path.
"""
from __future__ import annotations



import logging
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Literal

WatchBias = Literal["short", "long", "both", "wait"]

# Phase 3D: stale dump_active → accumulation when fall_from_high stops growing.
_DUMP_ACTIVE_MAX_DURATION_S = 2 * 3600.0
_FALL_STABLE_EPS_PCT = 0.20


@dataclass(slots=True)
class _DumpGuardState:
    last_fall: float = 0.0
    stable_since: float | None = None


_log = logging.getLogger(__name__)


def _resolve_state(state: Any | None) -> Any:
    if state is not None:
        return state
    from hunt_core.runtime.state import current_symbol_state

    return current_symbol_state()


def pre_dump_zone(
    *,
    price: float,
    hunt_high: float,
    fall_from_high_pct: float,
    symbol: str = "",
) -> bool:
    """Dump *start* window: fall ≤3% or still within ~5% headroom to hunt_high."""
    from hunt_core.params.store import lifecycle_thresholds

    th = lifecycle_thresholds(symbol)
    max_fall = float(th.get("pre_dump_max_fall_pct", 3.0))
    headroom_pct = float(th.get("pre_dump_headroom_pct", 5.0))
    if fall_from_high_pct <= max_fall:
        return True
    if hunt_high <= 0 or price <= 0:
        return fall_from_high_pct <= headroom_pct
    headroom = max(0.0, (hunt_high - price) / hunt_high * 100.0)
    return headroom <= headroom_pct and fall_from_high_pct <= headroom_pct


def _apply_dump_active_max_duration_guard(
    symbol: str,
    phase: HuntPhase,
    fall_from_high: float,
    *,
    now: float | None = None,
    state: Any | None = None,
) -> tuple[HuntPhase, str | None]:
    """If dump persists but fall plateaus >2h, allow accumulation (price basing)."""
    store = _resolve_state(state)
    sym = (symbol or "_").upper()
    ts = now if now is not None else time.monotonic()
    if phase != HuntPhase.DUMP_ACTIVE:
        store.dump_guard.pop(sym, None)
        return phase, None

    st = store.dump_guard.setdefault(sym, _DumpGuardState())
    if fall_from_high > st.last_fall + _FALL_STABLE_EPS_PCT:
        st.stable_since = None
    elif st.stable_since is None:
        st.stable_since = ts
    st.last_fall = fall_from_high

    if st.stable_since is not None and (ts - st.stable_since) >= _DUMP_ACTIVE_MAX_DURATION_S:
        return HuntPhase.ACCUMULATION, (
            f"dump_active_stabilized_fall{fall_from_high:.1f}%_"
            f"stable_h={(ts - st.stable_since) / 3600:.1f}"
        )
    return phase, None

HuntPhaseName = Literal[
    "exhaustion_at_high",
    "distribution",
    "dump_initiating",
    "dump_active",
    "post_dump_bounce",
    "recovery",
    "accumulation",
    "breakout_arming",
    "impulse_initiating",
    "no_setup",
]


class HuntPhase(StrEnum):
    EXHAUSTION_AT_HIGH = "exhaustion_at_high"
    DISTRIBUTION = "distribution"
    DUMP_INITIATING = "dump_initiating"
    DUMP_ACTIVE = "dump_active"
    POST_DUMP_BOUNCE = "post_dump_bounce"
    RECOVERY = "recovery"
    ACCUMULATION = "accumulation"
    BREAKOUT_ARMING = "breakout_arming"
    IMPULSE_INITIATING = "impulse_initiating"
    NO_SETUP = "no_setup"


@dataclass(frozen=True, slots=True)
class HuntLifecycle:
    phase: HuntPhase
    recommended_bias: WatchBias
    short_entry_ok: bool
    short_confirm_ok: bool
    invalidate_short: bool
    fall_from_high_pct: float
    bounce_from_low_pct: float
    local_support: float
    local_resistance: float
    reasons: tuple[str, ...]
    phase_4h: HuntPhase = HuntPhase.NO_SETUP
    regime: str = "range"
    regime_confidence: float = 0.0
    regime_previous: str | None = None
    regime_transitioned: bool = False


_HTF_VETO_LONG_PHASES = frozenset({HuntPhase.DISTRIBUTION, HuntPhase.DUMP_ACTIVE})
_HTF_VETO_SHORT_PHASES = frozenset(
    {
        HuntPhase.ACCUMULATION,
        HuntPhase.IMPULSE_INITIATING,
        HuntPhase.BREAKOUT_ARMING,
    }
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if v == v else default  # NaN guard
    except (TypeError, ValueError):
        return default


def _require_f(value: Any, field: str) -> tuple[float | None, str | None]:
    """Require numeric field — returns ``(value, None)`` or ``(None, data_missing_* )``."""
    if value is None:
        return None, f"data_missing_{field}"
    try:
        v = float(value)
        if v != v:
            return None, f"data_missing_{field}"
        return v, None
    except (TypeError, ValueError):
        return None, f"data_missing_{field}"


def _no_setup_lifecycle(
    reason: str,
    *,
    hunt_low: float,
    hunt_high: float,
) -> HuntLifecycle:
    return HuntLifecycle(
        phase=HuntPhase.NO_SETUP,
        recommended_bias="wait",
        short_entry_ok=False,
        short_confirm_ok=False,
        invalidate_short=False,
        fall_from_high_pct=0.0,
        bounce_from_low_pct=0.0,
        local_support=hunt_low,
        local_resistance=hunt_high,
        reasons=(reason,),
        phase_4h=HuntPhase.NO_SETUP,
    )


def _rsi_exhaustion_active(
    symbol: str,
    rsi_1h: float,
    *,
    state: Any | None = None,
) -> bool:
    """2-point hysteresis band: enter >=66, exit <63, hold between."""
    from hunt_core.params.store import lifecycle_thresholds

    store = _resolve_state(state)
    th = lifecycle_thresholds(symbol)
    enter = float(th.get("rsi_exhaustion_enter", 66.0))
    exit_ = float(th.get("rsi_exhaustion_exit", 63.0))
    sym = (symbol or "_").upper()
    latched = store.rsi_exhaustion_latched.get(sym, False)
    if latched:
        if rsi_1h < exit_:
            latched = False
    elif rsi_1h >= enter:
        latched = True
    store.rsi_exhaustion_latched[sym] = latched
    return latched


def _local_pivot_support(tf: dict[str, Any], *, impulse_low: float, session_low: float) -> float:
    """Recent higher-low proxy — not the impulse leg high (BEAT bug)."""
    candidates: list[float] = []
    for key in ("15m_closed", "5m_closed"):
        block = tf.get(key) or {}
        if not block.get("closed_bar"):
            continue
        c = block.get("candle") or {}
        lo = _f(c.get("low"))
        if lo > 0:
            candidates.append(lo)
    pivot = max(candidates) if candidates else 0.0
    floor = max(impulse_low, session_low, 0.0)
    if pivot <= floor:
        return floor
    return pivot


def _local_pivot_resistance(tf: dict[str, Any], *, impulse_high: float) -> float:
    """Recent lower-high proxy from closed LTF bars — NOT the impulse high.

    Anchoring SL to impulse high mid-dump produced 24-53% stops (EPIC/STG bug):
    the impulse high can be a 4h leg 20-50% above price. The local pivot is the
    nearest closed 5m/15m structure; impulse high is only the fallback.
    """
    candidates: list[float] = []
    for key in ("5m_closed", "15m_closed"):
        block = tf.get(key) or {}
        if not block.get("closed_bar"):
            continue
        hi = _f((block.get("candle") or {}).get("high"))
        if hi > 0:
            candidates.append(hi)
        prev_hi = _f(block.get("prev_high"))
        if prev_hi > 0:
            candidates.append(prev_hi)
    if not candidates:
        return impulse_high
    return min(candidates)


def _range_touch_bars(tf: dict[str, Any]) -> list[tuple[float, float]]:
    bars: list[tuple[float, float]] = []
    for key in ("5m_closed", "15m_closed", "1h_closed"):
        block = tf.get(key) or {}
        if not block.get("closed_bar"):
            continue
        if not isinstance(block, dict):
            continue
        candle = block.get("candle") or {}
        hi = _f(candle.get("high"))
        lo = _f(candle.get("low"))
        if hi > 0 and lo > 0:
            bars.append((hi, lo))
        prev_hi = _f(block.get("prev_high"))
        prev_lo = _f(block.get("prev_low"))
        if prev_hi > 0 and prev_lo > 0:
            bars.append((prev_hi, prev_lo))
    return bars


def _accumulation_touch_total(
    tf: dict[str, Any],
    market: dict[str, Any],
    *,
    range_high: float,
    range_low: float,
) -> int:
    """Boundary touch count for accumulation gate (Phase 4C)."""
    pre = market.get("range_touch_total")
    if pre is not None:
        try:
            return int(pre)
        except (TypeError, ValueError):
            pass
    frame = market.get("frame_15m")
    if frame is not None:
        try:
            from hunt_core.features.volume_profile import count_range_touches

            _, _, total = count_range_touches(
                frame,
                range_high=range_high,
                range_low=range_low,
                lookback=96,
            )
            return total
        except Exception:
            _log.warning(
                "accumulation_touch_count_frame_failed",
                exc_info=True,
            )
            return 0
    from hunt_core.features.volume_profile import count_range_touches_from_bars

    bars = _range_touch_bars(tf)
    if not bars:
        return 0
    _, _, total = count_range_touches_from_bars(
        bars,
        range_high=range_high,
        range_low=range_low,
    )
    return total


def assess_4h_lifecycle_phase(
    *,
    price: float,
    hunt_high: float,
    hunt_low: float,
    tf: dict[str, Any],
    session: dict[str, Any] | None = None,
) -> HuntPhase:
    """Coarse HTF lifecycle from 4h frame + hunt extremes (Phase 13B)."""
    r4h = tf.get("4h_closed") or tf.get("4h") or {}
    if not isinstance(r4h, dict) or r4h.get("status") == "empty":
        return HuntPhase.NO_SETUP
    if price <= 0 or hunt_high <= 0:
        return HuntPhase.NO_SETUP

    from hunt_core.analysis.trend_engine import trend_1h_bias

    sess = session or {}
    pos, pos_err = _require_f(sess.get("pos_in_range"), "pos_in_range")
    if pos_err:
        return HuntPhase.NO_SETUP
    fall_from_high = max(0.0, (hunt_high - price) / hunt_high) * 100.0
    rsi_4h, rsi_err = _require_f(r4h.get("rsi14"), "rsi4h")
    if rsi_err:
        return HuntPhase.NO_SETUP
    near_high = price >= hunt_high * 0.97 or pos >= 0.82
    trend = trend_1h_bias(r4h)

    # COAI lesson: parabolic memecoins keep high pos_in_range while falling hard off hunt_high.
    if fall_from_high >= 15.0:
        return HuntPhase.DUMP_ACTIVE
    if fall_from_high >= 12.0 and pos < 0.55:
        return HuntPhase.DUMP_ACTIVE
    if near_high and rsi_4h >= 65.0:
        return HuntPhase.DISTRIBUTION if fall_from_high >= 5.0 else HuntPhase.EXHAUSTION_AT_HIGH
    if trend == "bull" and fall_from_high < 8.0 and rsi_4h < 68.0 and pos >= 0.40:
        return HuntPhase.IMPULSE_INITIATING
    if pos <= 0.40 and fall_from_high >= 8.0 and hunt_low > 0:
        return HuntPhase.ACCUMULATION
    if fall_from_high >= 8.0 and pos < 0.70:
        return HuntPhase.DISTRIBUTION
    if trend == "bull" and fall_from_high < 12.0 and pos >= 0.35:
        return HuntPhase.BREAKOUT_ARMING
    return HuntPhase.NO_SETUP


def htf_bias_override(
    phase_4h: HuntPhase | str,
    direction: Literal["long", "short"],
) -> tuple[bool, str]:
    """Hard veto from 4h HTF phase (Phase 13B)."""
    try:
        ph = phase_4h if isinstance(phase_4h, HuntPhase) else HuntPhase(str(phase_4h))
    except ValueError:
        return False, ""
    d = direction.lower().strip()
    if d == "long" and ph in _HTF_VETO_LONG_PHASES:
        return True, f"htf_4h_{ph.value}_vs_long"
    if d == "short" and ph in _HTF_VETO_SHORT_PHASES:
        return True, f"htf_4h_{ph.value}_vs_short"
    return False, ""


def assess_hunt_lifecycle(
    *,
    price: float,
    hunt_high: float,
    hunt_low: float,
    session: dict[str, Any],
    tf: dict[str, Any],
    market: dict[str, Any],
    symbol: str = "",
    state: Any | None = None,
) -> HuntLifecycle:
    """Classify memecoin leg phase for short/long gating."""
    from hunt_core.params.store import lifecycle_thresholds

    th = lifecycle_thresholds(symbol)
    meaningful_dump_pct = th.get("meaningful_dump_pct", 8.0)
    pre_dump_max_fall = float(th.get("pre_dump_max_fall_pct", 3.0))
    pre_dump_headroom = float(th.get("pre_dump_headroom_pct", 5.0))
    violent_dump_pct = float(th.get("violent_dump_fall_pct", 15.0))
    fast_dump_pct = float(th.get("fast_dump_fall_pct", 12.0))
    parabolic_leg_pct = th.get("parabolic_leg_gain_pct", 20.0)
    mega_leg_pct = th.get("mega_leg_gain_pct", 80.0)
    near_high_pos = th.get("near_high_pos", 0.82)
    near_high_ratio = th.get("near_high_price_ratio", 0.97)
    post_dump_pos = th.get("post_dump_bounce_pos", 0.55)
    bounce_floor = th.get("bounce_min_floor_pct", 5.0)
    bounce_atr_mult = th.get("bounce_min_atr_mult", 1.5)
    rsi_ob = th.get("rsi_1h_overbought", 65.0)
    taker_buy_min = th.get("taker_buy_min", 1.05)
    taker_sell_max = th.get("taker_sell_max", 0.98)
    cascade_wick_min = th.get("cascade_wick_ratio_min", 0.25)
    squeeze_bb_max = th.get("squeeze_bb_width_pctile_max", 0.25)
    squeeze_don_max = th.get("squeeze_donchian_width_pct_max", 10.0)

    if price <= 0 or hunt_high <= 0:
        return HuntLifecycle(
            phase=HuntPhase.NO_SETUP,
            recommended_bias="wait",
            short_entry_ok=False,
            short_confirm_ok=False,
            invalidate_short=False,
            fall_from_high_pct=0.0,
            bounce_from_low_pct=0.0,
            local_support=hunt_low,
            local_resistance=hunt_high,
            reasons=("missing_price_or_high",),
            phase_4h=HuntPhase.NO_SETUP,
        )

    sess_hi = _f(session.get("high_24h"), hunt_high)
    sess_lo = _f(session.get("low_24h"), hunt_low)
    pos, pos_err = _require_f(session.get("pos_in_range"), "pos_in_range")
    if pos_err:
        return _no_setup_lifecycle(pos_err, hunt_low=hunt_low, hunt_high=hunt_high)

    fall_from_high = max(0.0, (hunt_high - price) / hunt_high) * 100.0
    # rally_from_24h_low — NOT "bounce after dump". On BEAT +400% this was +33% while
    # price was still in a parabolic leg, mislabeling post_dump_bounce + long bias.
    rally_from_24h_low = max(0.0, (price - sess_lo) / sess_lo) * 100.0 if sess_lo > 0 else 0.0
    # Post-dump bounce from impulse leg low — NOT 24h session rally (BEAT +400% lesson).
    if hunt_low > 0 and fall_from_high >= pre_dump_max_fall:
        bounce_from_low = max(0.0, (price - hunt_low) / hunt_low) * 100.0
    else:
        bounce_from_low = 0.0
    leg_gain_pct = (
        max(0.0, (hunt_high - hunt_low) / hunt_low) * 100.0 if hunt_low > 0 else 0.0
    )
    near_high = price >= hunt_high * near_high_ratio or pos >= near_high_pos
    at_pre_dump = pre_dump_zone(
        price=price,
        hunt_high=hunt_high,
        fall_from_high_pct=fall_from_high,
        symbol=symbol,
    )
    meaningful_dump = fall_from_high >= meaningful_dump_pct
    mega_parabolic_leg = leg_gain_pct >= mega_leg_pct
    upward_leg_shallow_pullback = leg_gain_pct >= parabolic_leg_pct and fall_from_high < meaningful_dump_pct

    r1h = tf.get("1h") or {}
    r5_block = tf.get("5m_closed") or {}
    if not r5_block.get("closed_bar"):
        r5_block = {}
    c5 = (r5_block.get("candle") or {}) if isinstance(r5_block.get("candle"), dict) else {}
    taker = market.get("taker_5m") or market.get("taker_1h")
    micro = market.get("microprice_bias")

    atr1h_pct, atr_err = _require_f(r1h.get("atr_pct"), "atr1h")
    if atr_err:
        return _no_setup_lifecycle(atr_err, hunt_low=hunt_low, hunt_high=hunt_high)
    # Hysteresis: one 5m memecoin candle is 3%+ — a "bounce" must beat ATR noise
    # (HYPE phase_change x5/hour churn lesson). Threshold = max(5%, 1.5 x ATR1h%).
    bounce_min = max(bounce_floor, bounce_atr_mult * atr1h_pct)

    local_support = _local_pivot_support(tf, impulse_low=hunt_low, session_low=sess_lo)
    local_resistance = _local_pivot_resistance(tf, impulse_high=hunt_high)

    reasons: list[str] = []
    rsi_1h, rsi_err = _require_f(r1h.get("rsi14"), "rsi1h")
    if rsi_err:
        return _no_setup_lifecycle(rsi_err, hunt_low=hunt_low, hunt_high=hunt_high)
    rsi_exhaustion = _rsi_exhaustion_active(symbol, rsi_1h, state=state)
    r1h_closed_div = tf.get("1h_closed") or {}
    if not r1h_closed_div.get("closed_bar"):
        r1h_closed_div = {}
    r4h_closed_div = tf.get("4h_closed") or {}
    if not r4h_closed_div.get("closed_bar"):
        r4h_closed_div = {}
    has_bear_div = bool(
        r1h_closed_div.get("bearish_rsi_div")
        or r4h_closed_div.get("bearish_rsi_div")
    )

    # --- phase detection ---
    taker_buy = taker is not None and taker > taker_buy_min
    taker_sell = taker is not None and taker < taker_sell_max
    bull_cascade = c5.get("bullish") and _f(c5.get("lower_wick_ratio")) >= cascade_wick_min
    bear_cascade = c5.get("bearish") and _f(c5.get("upper_wick_ratio")) >= cascade_wick_min

    r1h_pctile = r1h.get("bb_width_pctile")
    r1h_don = r1h.get("donchian_width_pct")
    squeeze_charged = (
        r1h_pctile is not None
        and r1h_don is not None
        and float(r1h_pctile) <= squeeze_bb_max
        and float(r1h_don) <= squeeze_don_max
    )

    # WS liquidation rollups: score 0→1 = long/short liq share (short liq → score↑).
    liq_score_raw = market.get("liquidation_score_5m")
    liq_score: float | None = None
    if liq_score_raw is not None:
        try:
            liq_score = float(liq_score_raw)
            if liq_score != liq_score:
                liq_score = None
        except (TypeError, ValueError):
            liq_score = None
    liq_long_n = _f(market.get("liquidation_long_notional_5m"))
    liq_short_n = _f(market.get("liquidation_short_notional_5m"))
    liq_total = liq_long_n + liq_short_n
    liq_min_notional = 50_000.0
    short_squeeze_active = (
        liq_score is not None and liq_score >= 0.70 and liq_total >= liq_min_notional
    )
    long_squeeze_active = (
        liq_score is not None and liq_score <= 0.30 and liq_total >= liq_min_notional
    )
    if short_squeeze_active and liq_score is not None:
        reasons.append(f"short_squeeze_liq={liq_score:.2f}_${liq_total:.0f}")
    if long_squeeze_active and liq_score is not None:
        reasons.append(f"long_squeeze_liq={liq_score:.2f}_${liq_total:.0f}")

    # Initial pump leg — BEAT Jun6-8 / VELVET Jun7-10: catch BEFORE exhaustion fade zone.
    pre_dump_topping = at_pre_dump and (
        has_bear_div
        or bear_cascade
        or rsi_exhaustion
        or rsi_1h >= rsi_ob
    )
    impulse_rally = (
        leg_gain_pct >= parabolic_leg_pct
        and rally_from_24h_low >= bounce_min
        and not at_pre_dump
        and not meaningful_dump
        and not pre_dump_topping
        and 0.40 <= pos < 0.75
        and rsi_1h < rsi_ob
        and (taker_buy or bull_cascade or (micro is not None and micro > 0))
    )
    # COAI/RIF: fall from hunt_high dominates pos_in_range on parabolic memecoins.
    bounce_recovery_candidate = (
        meaningful_dump
        and bounce_from_low >= bounce_min
        and pos >= post_dump_pos
        and (taker_buy or bull_cascade)
        and fall_from_high < 18.0
    )
    if fall_from_high >= 18.0:
        phase = HuntPhase.DUMP_ACTIVE
        reasons.append(f"violent_dump_fall={fall_from_high:.1f}%")
    elif fall_from_high >= violent_dump_pct and not bounce_recovery_candidate:
        phase = HuntPhase.DUMP_ACTIVE
        reasons.append(f"violent_dump_fall={fall_from_high:.1f}%")
    elif (
        fall_from_high >= fast_dump_pct
        and (taker_sell or bear_cascade or long_squeeze_active)
        and not near_high
    ):
        phase = HuntPhase.DUMP_ACTIVE
        reasons.append(f"fast_dump_fall={fall_from_high:.1f}%_bear_flow")
    elif pre_dump_topping:
        # Pre-dump: fall ≤3% or ≤5% headroom — even if taker still buy-side (COAI top lesson).
        if has_bear_div or bear_cascade:
            phase = HuntPhase.DISTRIBUTION
            reasons.append(
                f"pre_dump_topping_div_fall{fall_from_high:.1f}%_headroom"
                f"{max(0.0, (hunt_high - price) / hunt_high * 100.0 if hunt_high else 0):.1f}%"
            )
        else:
            phase = HuntPhase.EXHAUSTION_AT_HIGH
            reasons.append(f"pre_dump_exhaustion_fall{fall_from_high:.1f}%")
    elif (
        near_high
        and fall_from_high < meaningful_dump_pct
        and (
            has_bear_div
            or bear_cascade
            or rsi_exhaustion
            or rsi_1h >= rsi_ob
        )
        and not (taker_buy and fall_from_high < 2.0 and not has_bear_div)
    ):
        # Legacy topping path (slightly wider than strict pre_dump window).
        if has_bear_div and (bear_cascade or taker_sell or (taker is not None and taker < 1.02)):
            phase = HuntPhase.DISTRIBUTION
            reasons.append(f"topping_forming_div_fall{fall_from_high:.1f}%")
        else:
            phase = HuntPhase.EXHAUSTION_AT_HIGH
            reasons.append(f"topping_exhaustion_fall{fall_from_high:.1f}%")
    elif impulse_rally:
        phase = HuntPhase.IMPULSE_INITIATING
        reasons.append(
            f"impulse_initiating_leg{leg_gain_pct:.0f}%_pos={pos:.2f}_rally{rally_from_24h_low:.1f}%"
        )
    elif (
        (squeeze_charged or short_squeeze_active)
        and 0.30 <= pos <= 0.65
        and 8.0 <= leg_gain_pct < parabolic_leg_pct
        and rally_from_24h_low >= bounce_min * 0.6
    ):
        phase = HuntPhase.BREAKOUT_ARMING
        reasons.append(
            f"breakout_arming_squeeze_leg{leg_gain_pct:.0f}%_pos={pos:.2f}"
        )
    elif upward_leg_shallow_pullback:
        # Parabolic top: near ATH + RSI OB → fade; mid-leg still pumping → impulse not distribution.
        if near_high and (rsi_exhaustion or rsi_1h >= rsi_ob):
            phase = HuntPhase.EXHAUSTION_AT_HIGH
            reasons.append(
                f"parabolic_exhaustion_leg{leg_gain_pct:.0f}%_rally{rally_from_24h_low:.1f}%"
            )
        elif pos < 0.75 and (taker_buy or bull_cascade):
            phase = HuntPhase.IMPULSE_INITIATING
            reasons.append(
                f"parabolic_impulse_leg{leg_gain_pct:.0f}%_fall{fall_from_high:.1f}%"
            )
        else:
            phase = HuntPhase.DISTRIBUTION
            reasons.append(
                f"parabolic_distribution_leg{leg_gain_pct:.0f}%_fall{fall_from_high:.1f}%"
            )
    elif (
        mega_parabolic_leg
        and fall_from_high < 18.0
        and rally_from_24h_low >= bounce_min
    ):
        # BEAT Jun10 5→8.37: +362% leg — shallow dip off hunt_high is NOT post_dump_bounce.
        if near_high and (rsi_exhaustion or rsi_1h >= rsi_ob):
            phase = HuntPhase.EXHAUSTION_AT_HIGH
            reasons.append(
                f"mega_leg_exhaustion_{leg_gain_pct:.0f}%_fall{fall_from_high:.1f}%"
            )
        elif (
            at_pre_dump
            and has_bear_div
            and near_high
        ):
            phase = HuntPhase.DISTRIBUTION
            reasons.append(
                f"mega_pre_dump_div_leg{leg_gain_pct:.0f}%_fall{fall_from_high:.1f}%"
            )
        elif (
            fall_from_high < violent_dump_pct
            and not (at_pre_dump and has_bear_div)
            and (taker_buy or bull_cascade or (micro is not None and micro > 0))
            and (fall_from_high < 12.0 or not near_high)
        ):
            phase = HuntPhase.IMPULSE_INITIATING
            reasons.append(
                f"mega_leg_continuation_{leg_gain_pct:.0f}%_fall{fall_from_high:.1f}%"
                f"_rally{rally_from_24h_low:.1f}%"
            )
        elif near_high:
            phase = HuntPhase.DISTRIBUTION
            reasons.append(
                f"mega_leg_topping_{leg_gain_pct:.0f}%_fall{fall_from_high:.1f}%"
            )
        else:
            phase = HuntPhase.IMPULSE_INITIATING
            reasons.append(
                f"mega_leg_impulse_pos{pos:.2f}_leg{leg_gain_pct:.0f}%"
            )
    elif (
        meaningful_dump
        and bounce_from_low >= bounce_min
        and pos >= post_dump_pos
        and (
            fall_from_high < violent_dump_pct
            or bounce_recovery_candidate
        )
    ):
        # True post-dump recovery only after >=8% off hunt_high on NON-mega legs (JCT/VELVET).
        if taker_buy or bull_cascade or (micro is not None and micro > 0):
            phase = HuntPhase.POST_DUMP_BOUNCE
            reasons.append(
                f"post_dump_bounce_fall{fall_from_high:.1f}%_pos={pos:.2f}"
            )
            if taker_buy:
                reasons.append(f"taker_buy={taker:.2f}")
        elif pos >= 0.75 and fall_from_high < 18.0:
            phase = HuntPhase.RECOVERY
            reasons.append(f"recovery_pos={pos:.2f}_off_high={fall_from_high:.1f}%")
        else:
            phase = HuntPhase.POST_DUMP_BOUNCE
            reasons.append(f"post_dump_bounce_fall{fall_from_high:.1f}%")
    elif (
        meaningful_dump
        and pos >= 0.70
        and fall_from_high < violent_dump_pct
        and fall_from_high < 18.0
        and (taker_buy or bull_cascade)
    ):
        phase = HuntPhase.RECOVERY
        reasons.append(f"squeeze_recovery_pos={pos:.2f}_fall{fall_from_high:.1f}%")
    elif near_high and rsi_exhaustion:
        phase = HuntPhase.EXHAUSTION_AT_HIGH
        reasons.append(f"near_high_pos={pos:.2f}_rsi1h={rsi_1h:.0f}")
    elif (
        long_squeeze_active
        and fall_from_high >= 5.0
        and pos < 0.65
    ):
        phase = HuntPhase.DUMP_ACTIVE
        reasons.append(f"long_squeeze_cascade_fall{fall_from_high:.1f}%")
    elif (
        fall_from_high >= meaningful_dump_pct
        and pos < 0.55
        and (taker_sell or bear_cascade or long_squeeze_active)
    ):
        phase = HuntPhase.DUMP_ACTIVE
        reasons.append(f"fall_from_high={fall_from_high:.1f}%")
    elif fall_from_high >= 12.0 and pos < 0.55:
        phase = HuntPhase.DUMP_ACTIVE
        reasons.append(f"mid_dump_fall={fall_from_high:.1f}%_pos={pos:.2f}")
    elif (
        meaningful_dump
        and not mega_parabolic_leg
        and bounce_from_low >= 15.0
        and fall_from_high >= 10.0
        and fall_from_high < violent_dump_pct
    ):
        phase = HuntPhase.POST_DUMP_BOUNCE
        reasons.append(
            f"deep_post_dump_bounce_fall{fall_from_high:.1f}%_bounce{bounce_from_low:.1f}%"
        )
    elif fall_from_high >= 2.0 and fall_from_high < 10.0 and pos >= 0.55 and bear_cascade:
        phase = HuntPhase.DISTRIBUTION
        reasons.append("distribution_break_forming")
    elif pos <= 0.35 and rsi_1h <= 40:
        if fall_from_high >= 12.0:
            phase = HuntPhase.DUMP_ACTIVE
            reasons.append(f"low_pos_mid_dump_fall={fall_from_high:.1f}%")
        else:
            range_hi = local_resistance if local_resistance > 0 else sess_hi
            range_lo = sess_lo if sess_lo > 0 else hunt_low
            total_touches = _accumulation_touch_total(
                tf,
                market,
                range_high=range_hi,
                range_low=range_lo,
            )
            if total_touches >= 4:
                phase = HuntPhase.ACCUMULATION
                reasons.append(f"post_dump_accumulation_touches={total_touches}")
            else:
                phase = HuntPhase.NO_SETUP
                reasons.append(f"accumulation_pending_touches={total_touches}/4")
    elif (
        rally_from_24h_low >= 12.0
        and 0.40 <= pos < 0.80
        and fall_from_high < fast_dump_pct
        and rsi_1h < 68
        and (taker_buy or bull_cascade or (micro is not None and micro > 0))
    ):
        phase = HuntPhase.IMPULSE_INITIATING
        reasons.append(f"rally_impulse_{rally_from_24h_low:.1f}%_pos={pos:.2f}")
    elif (
        fall_from_high >= pre_dump_max_fall
        and fall_from_high < fast_dump_pct
    ):
        phase = HuntPhase.DUMP_INITIATING
        reasons.append(f"dump_initiating_fall{fall_from_high:.1f}%")
    else:
        phase = HuntPhase.NO_SETUP
        reasons.append("no_clear_phase")

    guarded_phase, guard_reason = _apply_dump_active_max_duration_guard(
        symbol, phase, fall_from_high, state=state
    )
    if guard_reason:
        if guarded_phase == HuntPhase.ACCUMULATION:
            range_hi = local_resistance if local_resistance > 0 else sess_hi
            range_lo = sess_lo if sess_lo > 0 else hunt_low
            guard_touches = _accumulation_touch_total(
                tf,
                market,
                range_high=range_hi,
                range_low=range_lo,
            )
            if guard_touches >= 4:
                phase = guarded_phase
                reasons.append(f"{guard_reason}_touches={guard_touches}")
            else:
                phase = HuntPhase.DUMP_ACTIVE
                reasons.append(
                    f"accumulation_pending_touches={guard_touches}/4_after_{guard_reason}"
                )
        else:
            phase = guarded_phase
            reasons.append(guard_reason)

    support_failed = local_support > 0 and price < local_support * 0.997
    if support_failed and phase in {
        HuntPhase.POST_DUMP_BOUNCE,
        HuntPhase.RECOVERY,
        HuntPhase.ACCUMULATION,
    }:
        if fall_from_high >= 4.0 or taker_sell or bear_cascade:
            phase = HuntPhase.DUMP_ACTIVE
            reasons.append(f"bounce_failed_support={local_support:.4f}")
        elif fall_from_high >= 2.0:
            phase = HuntPhase.DISTRIBUTION
            reasons.append(f"support_break_forming={local_support:.4f}")

    # Invalidate a LIVE short only on real structure: a CLOSED 15m bullish bar
    # closing above the prior 15m high. Phase flips alone (one 5m bounce) used to
    # kill 4h dumps in minutes (JCT -21% / 5-min signal-life churn lesson).
    c15c = tf.get("15m_closed") or {}
    c15_candle = (c15c.get("candle") or {}) if isinstance(c15c.get("candle"), dict) else {}
    prev_15m_high = _f(c15c.get("prev_high"))
    closed_15m_break_up = (
        bool(c15c.get("closed_bar"))
        and bool(c15_candle.get("bullish"))
        and prev_15m_high > 0
        and _f(c15_candle.get("close")) > prev_15m_high
    )
    bounce_phase = phase in {
        HuntPhase.POST_DUMP_BOUNCE,
        HuntPhase.RECOVERY,
        HuntPhase.ACCUMULATION,
    }
    invalidate_short = bounce_phase and closed_15m_break_up

    # RIF/COAI lesson: closed 5m+15m reclaimed effective support + bounce off 24h low
    # → short fade at exhaustion is structurally dead (independent long_bounce agrees).
    support_reclaim = False
    mega_bounce_invalidate = (
        phase in {HuntPhase.EXHAUSTION_AT_HIGH, HuntPhase.DISTRIBUTION}
        and bounce_from_low >= 15.0
        and fall_from_high < 8.0
        and not (has_bear_div and near_high)
    )
    if mega_bounce_invalidate:
        support_reclaim = True
        reasons.append(f"mega_bounce_invalidate={bounce_from_low:.1f}%")
    elif phase in {HuntPhase.EXHAUSTION_AT_HIGH, HuntPhase.DISTRIBUTION}:
        sup_break = _effective_support_for_phase(
            phase=phase,
            hunt_high=hunt_high,
            local_support=local_support,
            fall_from_high=fall_from_high,
            pos_in_range=pos,
        )
        still_below = _closed_bars_still_below_support(tf, sup_break)
        if bounce_from_low >= 2.0 and not still_below:
            if has_bear_div and near_high and fall_from_high < pre_dump_headroom:
                reasons.append(
                    f"topping_bear_div_keeps_short_bounce={bounce_from_low:.1f}%"
                )
            else:
                support_reclaim = True
                reasons.append(f"support_reclaim_bounce={bounce_from_low:.1f}%")
        else:
            prem_blocked, prem_reason = blocks_premature_exhaustion_short(
                phase=phase.value,
                fall_from_high_pct=fall_from_high,
                bounce_from_low_pct=bounce_from_low,
                pos_in_range=pos,
                has_bear_div=has_bear_div,
                symbol=symbol,
            )
            if prem_blocked:
                support_reclaim = True
                reasons.append(f"premature_fade:{prem_reason}")

    if support_reclaim:
        invalidate_short = True

    if bounce_phase and not closed_15m_break_up:
        reasons.append("bounce_unconfirmed_15m")
    short_entry_ok = phase in {
        HuntPhase.EXHAUSTION_AT_HIGH,
        HuntPhase.DISTRIBUTION,
        HuntPhase.DUMP_INITIATING,
    }
    if support_reclaim:
        short_entry_ok = False
    short_confirm_ok = (
        phase
        in {
            HuntPhase.EXHAUSTION_AT_HIGH,
            HuntPhase.DISTRIBUTION,
            HuntPhase.DUMP_INITIATING,
            HuntPhase.DUMP_ACTIVE,
        }
        and not invalidate_short
    )

    if phase == HuntPhase.DUMP_ACTIVE:
        # VELVET lesson: mid-dump is valid *monitoring* but late TG entry loses (BEAT at 4.65).
        short_entry_ok = False
        reasons.append("no_new_short_entry_mid_dump")

    # P1.13: surface kinematic velocity Z as a lifecycle feature/signal (not a
    # gate). Velocity proxy from 1h/24h change mirrors gate.wash.kinematic_z so
    # phase telemetry carries the same chase-pace context the gate sees.
    from hunt_core.gate.delivery import kinematic_z

    chg_1h_pct = _f(r1h.get("change_pct") or r1h.get("price_change_pct"))
    chg_24h_pct = _f(market.get("chg_24h_pct") or session.get("change_24h_pct"))
    kin_v_z, _kin_a_z = kinematic_z(change_1h_pct=chg_1h_pct, change_24h_pct=chg_24h_pct)
    # Prepend so the Z survives the reasons[:8] cap at construction.
    reasons.insert(0, f"kinematic_v_z={kin_v_z:.2f}")
    if abs(kin_v_z) >= 3.0:
        reasons.insert(1, "kinematic_fast_leg")

    bias: WatchBias = "wait"
    if phase in {
        HuntPhase.EXHAUSTION_AT_HIGH,
        HuntPhase.DISTRIBUTION,
        HuntPhase.DUMP_INITIATING,
    }:
        bias = "wait" if invalidate_short else "short"
    elif phase in {
        HuntPhase.POST_DUMP_BOUNCE,
        HuntPhase.ACCUMULATION,
        HuntPhase.RECOVERY,
        HuntPhase.BREAKOUT_ARMING,
        HuntPhase.IMPULSE_INITIATING,
    }:
        bias = "long"
    elif phase == HuntPhase.DUMP_ACTIVE:
        bias = "wait"

    phase_4h = assess_4h_lifecycle_phase(
        price=price,
        hunt_high=hunt_high,
        hunt_low=hunt_low,
        tf=tf,
        session=session,
    )

    return HuntLifecycle(
        phase=phase,
        recommended_bias=bias,
        short_entry_ok=short_entry_ok,
        short_confirm_ok=short_confirm_ok,
        invalidate_short=invalidate_short,
        fall_from_high_pct=round(fall_from_high, 2),
        bounce_from_low_pct=round(bounce_from_low, 2),
        local_support=round(local_support, 6),
        local_resistance=round(local_resistance, 6),
        reasons=tuple(reasons[:8]),
        phase_4h=phase_4h,
    )


def _closed_bar_close(tf_closed: dict[str, Any]) -> float:
    if not isinstance(tf_closed, dict) or not tf_closed.get("closed_bar"):
        return 0.0
    candle = tf_closed.get("candle") or {}
    if isinstance(candle, dict) and candle.get("close") is not None:
        return _f(candle.get("close"))
    return _f(tf_closed.get("close"))


def _closed_bars_still_below_support(tf: dict[str, Any], support: float) -> bool:
    """True when both available closed bars remain below *support* (beat_check parity)."""
    if support <= 0:
        return False
    c5 = _closed_bar_close(tf.get("5m_closed") or {})
    c15 = _closed_bar_close(tf.get("15m_closed") or {})
    if c5 <= 0 and c15 <= 0:
        return True  # no closed bars — do not infer reclaim
    below5 = c5 > 0 and c5 < support
    below15 = c15 > 0 and c15 < support
    if c5 > 0 and c15 > 0:
        return below5 and below15
    if c5 > 0:
        return below5
    return below15


def _effective_support_for_phase(
    *,
    phase: HuntPhase,
    hunt_high: float,
    local_support: float,
    fall_from_high: float,
    pos_in_range: float,
) -> float:
    if phase == HuntPhase.EXHAUSTION_AT_HIGH:
        return round(hunt_high * 0.998, 6)
    if phase == HuntPhase.DISTRIBUTION:
        if fall_from_high >= 8.0:
            return round(max(local_support, hunt_high * 0.998), 6)
        return local_support
    if phase == HuntPhase.DUMP_INITIATING:
        return local_support
    if fall_from_high >= 8.0 and pos_in_range >= 0.55:
        return round(max(local_support, hunt_high * 0.998), 6)
    return local_support


def effective_support_break(
    *,
    impulse_high: float,
    lifecycle: HuntLifecycle,
    pos_in_range: float = 0.5,
) -> float:
    """Support for confirm: local pivot in distribution; impulse high only at exhaustion.

    Parabolic distribution (fall < 8%): pinning to ATH*0.998 confirmed dump on a 5% wick
    while taker was still buy-side (BEAT audit). ATH anchor requires meaningful_dump.
    """
    if lifecycle.phase == HuntPhase.EXHAUSTION_AT_HIGH:
        return round(impulse_high * 0.998, 6)
    if lifecycle.phase == HuntPhase.DISTRIBUTION:
        if lifecycle.fall_from_high_pct >= 8.0:
            return round(max(lifecycle.local_support, impulse_high * 0.998), 6)
        return lifecycle.local_support
    if lifecycle.fall_from_high_pct >= 8.0 and pos_in_range >= 0.55:
        return round(max(lifecycle.local_support, impulse_high * 0.998), 6)
    return lifecycle.local_support


def promote_initial_pump_lifecycle(row: dict[str, Any], *, symbol: str = "") -> None:
    """Upgrade lifecycle when ignition / breakout says initial pump (not post-dump).

    JSONL forensics: VELVET ticks at +14% chg24 had long fuel 50 but phase=no_setup
    because early rows lacked impulse window — ignition bridge closes that gap.
    """
    from hunt_core.params.store import effective_hunt_params

    lc = row.get("lifecycle")
    if not isinstance(lc, dict):
        return
    phase = str(lc.get("phase") or "")
    if phase in {
        HuntPhase.IMPULSE_INITIATING.value,
        HuntPhase.BREAKOUT_ARMING.value,
        HuntPhase.EXHAUSTION_AT_HIGH.value,
    }:
        return
    sym = (symbol or str(row.get("symbol") or "")).upper()
    cal = effective_hunt_params(sym)
    ign = row.get("ignition") or {}
    chg = abs(float(row.get("chg_24h_pct") or 0))
    long_s = row.get("long") or {}
    fuel = float(long_s.get("long_fuel") or long_s.get("long_score") or 0)
    triggers = [str(t) for t in (long_s.get("triggers") or [])]
    broke = any("broke_resistance" in t for t in triggers)
    sess = row.get("session") or {}
    pos = float(sess.get("pos_in_range") or 0.5)
    ign_pct = float(ign.get("price_delta_pct") or 0)

    promote = False
    reason = ""
    if str(ign.get("direction") or "") == "pump" and ign_pct >= 2.0:
        if fuel >= cal.forming_min_score or chg >= cal.anomaly_min_chg_24h_pct:
            promote = True
            reason = f"ignition_pump_{ign_pct:.1f}%"
    elif (
        chg >= cal.anomaly_min_chg_24h_pct
        and fuel >= cal.forming_min_score
        and broke
        and 0.35 <= pos < 0.85
        and phase in {HuntPhase.NO_SETUP.value, HuntPhase.DISTRIBUTION.value}
    ):
        promote = True
        reason = "broke_resistance_anomaly"

    if not promote:
        return

    lc["phase"] = HuntPhase.IMPULSE_INITIATING.value
    lc["recommended_bias"] = "long"
    if not lc.get("bounce_from_low_pct"):
        lo = float(sess.get("low_24h") or 0)
        px = float(row.get("price") or 0)
        if lo > 0 and px > lo:
            lc["bounce_from_low_pct"] = round((px - lo) / lo * 100.0, 2)
    lc.setdefault("reasons", [])
    if isinstance(lc["reasons"], list):
        lc["reasons"] = [*lc["reasons"], reason][:8]
    row["lifecycle"] = lc
    if long_s:
        long_s["lifecycle_phase"] = HuntPhase.IMPULSE_INITIATING.value
        row["long"] = long_s


def apply_short_invalidation(
    confirmed: bool,
    hard: list[str],
    lifecycle: HuntLifecycle,
    *,
    dump: dict[str, Any],
) -> tuple[bool, list[str], str | None]:
    """Demote sticky dump_confirmed when lifecycle says bounce (BEAT fix)."""
    if not confirmed:
        return confirmed, hard, None
    if lifecycle.invalidate_short:
        note = f"lifecycle_{lifecycle.phase.value}"
        return False, list(hard), note
    if not lifecycle.short_confirm_ok:
        return False, hard, f"lifecycle_{lifecycle.phase.value}"
    # Late-entry guard for fade-at-top — not dump_active continuation (JCT lesson).
    if (
        lifecycle.fall_from_high_pct >= 10.0
        and not lifecycle.short_entry_ok
        and lifecycle.phase != HuntPhase.DUMP_ACTIVE
    ):
        return False, hard, "late_short_after_dump"
    return confirmed, hard, None


LIFECYCLE_INVALIDATE_SHORT_FUEL_CAP = 32.0


def apply_invalidate_short_fuel_cap(dump: dict[str, Any]) -> None:
    """Cap dump fuel/score and demote confirmed when lifecycle invalidates short."""
    fuel = float(dump.get("dump_fuel") or 0)
    cap = LIFECYCLE_INVALIDATE_SHORT_FUEL_CAP
    if fuel <= cap:
        return
    dump["dump_fuel"] = cap
    dump["dump_score"] = min(float(dump.get("dump_score") or 0), cap)
    dump["confirmed"] = False
    triggers = list(dump.get("triggers") or [])
    if "lifecycle_short_cap" not in triggers:
        triggers.append("lifecycle_short_cap")
    dump["triggers"] = triggers


def capped_setup_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["short", "long"],
    symbol: str,
    tf: dict[str, Any] | None,
    lifecycle: dict[str, Any] | None = None,
) -> float:
    """Fuel after cluster/prokol math and lifecycle invalidate_short cap."""
    from hunt_core.scan._confirm_shared import compute_setup_fuel

    fuel = compute_setup_fuel(setup, direction=direction, symbol=symbol, tf=tf)
    if (
        direction == "short"
        and lifecycle
        and lifecycle.get("invalidate_short")
    ):
        return min(fuel, LIFECYCLE_INVALIDATE_SHORT_FUEL_CAP)
    if direction == "short" and "lifecycle_short_cap" in list(setup.get("triggers") or []):
        return min(fuel, LIFECYCLE_INVALIDATE_SHORT_FUEL_CAP)
    return fuel


def blocks_premature_exhaustion_short(
    *,
    phase: str,
    fall_from_high_pct: float,
    bounce_from_low_pct: float,
    pos_in_range: float,
    has_bear_div: bool,
    symbol: str = "",
) -> tuple[bool, str]:
    """Block fade-at-top shorts when only LTF broke impulse-high (JCT/BTW lesson)."""
    from hunt_core.params.store import lifecycle_thresholds

    th = lifecycle_thresholds(symbol)
    pos_tight = th.get("premature_exhaustion_pos_tight", 0.92)
    bounce_tight = th.get("premature_exhaustion_bounce_tight_pct", 4.0)
    pos_lo = th.get("premature_exhaustion_pos", 0.85)
    bounce_lo = th.get("premature_exhaustion_bounce_pct", 15.0)

    if phase not in {
        HuntPhase.EXHAUSTION_AT_HIGH.value,
        HuntPhase.DISTRIBUTION.value,
    }:
        return False, ""
    if has_bear_div:
        return False, ""
    # Distribution + ≤3% fall: pre-dump start (COAI) — not premature fade.
    if phase == HuntPhase.DISTRIBUTION.value and fall_from_high_pct <= 3.0:
        return False, ""
    if pos_in_range > pos_tight and bounce_from_low_pct > bounce_tight:
        return (
            True,
            f"premature_exhaustion_pos={pos_in_range:.2f}_bounce={bounce_from_low_pct:.1f}%",
        )
    if pos_in_range > pos_lo and bounce_from_low_pct > bounce_lo:
        return (
            True,
            f"premature_exhaustion_pos={pos_in_range:.2f}_bounce={bounce_from_low_pct:.1f}%",
        )
    return False, ""


def attach_regime(
    lifecycle: HuntLifecycle,
    *,
    prepared: dict[str, Any],
    market: dict[str, Any],
    symbol: str = "",
    state: Any | None = None,
) -> HuntLifecycle:
    """Parallel regime classify — enriches lifecycle without altering phase logic."""
    from hunt_core.domain.regime_classifier import classify_regime

    rr = classify_regime(prepared, lifecycle, market, symbol=symbol, state=state)
    return replace(
        lifecycle,
        regime=rr.regime.value,
        regime_confidence=rr.confidence,
        regime_previous=rr.previous.value if rr.previous else None,
        regime_transitioned=rr.transitioned,
    )


def lifecycle_to_dict(
    lifecycle: HuntLifecycle,
    *,
    leg_gain_pct: float = 0.0,
) -> dict[str, Any]:
    """Serialize lifecycle (+ parallel regime fields) for TickRow."""
    return {
        "phase": lifecycle.phase.value,
        "recommended_bias": lifecycle.recommended_bias,
        "lifecycle_4h": lifecycle.phase_4h.value,
        "phase_4h": lifecycle.phase_4h.value,
        "short_entry_ok": lifecycle.short_entry_ok,
        "short_confirm_ok": lifecycle.short_confirm_ok,
        "short_monitor_ok": lifecycle.short_confirm_ok,
        "invalidate_short": lifecycle.invalidate_short,
        "fall_from_high_pct": lifecycle.fall_from_high_pct,
        "bounce_from_low_pct": lifecycle.bounce_from_low_pct,
        "leg_gain_pct": leg_gain_pct,
        "local_support": lifecycle.local_support,
        "local_resistance": lifecycle.local_resistance,
        "reasons": list(lifecycle.reasons),
        "regime": lifecycle.regime,
        "regime_confidence": lifecycle.regime_confidence,
        "regime_previous": lifecycle.regime_previous,
        "regime_transitioned": lifecycle.regime_transitioned,
    }

# --- delivery FSM (merged from lifecycle_fsm.py) ---
Direction = Literal["short", "long"]


class DeliveryStage(StrEnum):
    FORMING = "forming"
    ARMED = "armed"
    TRIGGERED = "triggered"
    ACTIVE = "active"
    RESOLVED = "resolved"


_STAGE_ORDER: tuple[DeliveryStage, ...] = (
    DeliveryStage.FORMING,
    DeliveryStage.ARMED,
    DeliveryStage.TRIGGERED,
    DeliveryStage.ACTIVE,
    DeliveryStage.RESOLVED,
)

_ALLOWED: dict[DeliveryStage, frozenset[DeliveryStage]] = {
    DeliveryStage.FORMING: frozenset({DeliveryStage.ARMED, DeliveryStage.TRIGGERED, DeliveryStage.RESOLVED}),
    DeliveryStage.ARMED: frozenset({DeliveryStage.TRIGGERED, DeliveryStage.FORMING, DeliveryStage.RESOLVED}),
    DeliveryStage.TRIGGERED: frozenset({DeliveryStage.ACTIVE, DeliveryStage.ARMED, DeliveryStage.RESOLVED}),
    DeliveryStage.ACTIVE: frozenset({DeliveryStage.RESOLVED, DeliveryStage.TRIGGERED}),
    DeliveryStage.RESOLVED: frozenset({DeliveryStage.FORMING}),
}


@dataclass(frozen=True, slots=True)
class DeliveryFsmState:
    stage: DeliveryStage
    direction: str
    setup_id: str
    transitioned: bool
    previous: DeliveryStage | None


@dataclass(slots=True)
class _FsmEntry:
    stage: str = DeliveryStage.FORMING.value
    direction: str = ""
    setup_id: str = ""


def _key(symbol: str, direction: str) -> str:
    return f"{symbol.upper()}:{direction.lower()}"


def _infer_target(
    setup: dict[str, Any],
    *,
    delivery_tier: str | None = None,
    tracker_active: bool = False,
    tracker_closed: bool = False,
) -> DeliveryStage:
    if tracker_closed:
        return DeliveryStage.RESOLVED
    if tracker_active:
        return DeliveryStage.ACTIVE
    tier = str(delivery_tier or "").upper()
    if tier == "TRIGGERED" or bool(setup.get("confirmed")):
        return DeliveryStage.TRIGGERED if tier != "ACTIVE" else DeliveryStage.ACTIVE
    if tier == "ARMED":
        return DeliveryStage.ARMED
    fuel = float(setup.get("dump_fuel") or setup.get("long_fuel") or setup.get("dump_score") or setup.get("long_score") or 0)
    if fuel > 0 or setup.get("phase"):
        return DeliveryStage.FORMING
    return DeliveryStage.FORMING


def advance_delivery_fsm(
    symbol: str,
    direction: Direction,
    setup: dict[str, Any] | None,
    *,
    delivery_tier: str | None = None,
    tracker_active: bool = False,
    tracker_closed: bool = False,
    setup_id: str = "",
    state: Any | None = None,
) -> DeliveryFsmState | None:
    """Advance per-symbol delivery FSM; returns None when setup empty."""
    if not setup:
        return None
    store = _resolve_state(state)
    sym = symbol.upper()
    d = direction.lower()
    k = _key(sym, d)
    entry = store.delivery_fsm.setdefault(k, _FsmEntry())
    if not isinstance(entry, _FsmEntry):
        entry = _FsmEntry()
        store.delivery_fsm[k] = entry

    try:
        prev = DeliveryStage(entry.stage)
    except ValueError:
        prev = DeliveryStage.FORMING

    target = _infer_target(
        setup,
        delivery_tier=delivery_tier,
        tracker_active=tracker_active,
        tracker_closed=tracker_closed,
    )
    sid = setup_id or str(setup.get("setup_id") or setup.get("phase") or "unknown")
    entry.direction = d
    entry.setup_id = sid

    if target == prev:
        return DeliveryFsmState(
            stage=prev,
            direction=d,
            setup_id=sid,
            transitioned=False,
            previous=None,
        )

    allowed = _ALLOWED.get(prev, frozenset())
    if target not in allowed and not (prev == DeliveryStage.FORMING and target == DeliveryStage.TRIGGERED):
        return DeliveryFsmState(
            stage=prev,
            direction=d,
            setup_id=sid,
            transitioned=False,
            previous=None,
        )

    entry.stage = target.value
    return DeliveryFsmState(
        stage=target,
        direction=d,
        setup_id=sid,
        transitioned=True,
        previous=prev,
    )


def record_delivery_fsm(
    symbol: str,
    direction: Direction,
    setup: dict[str, Any] | None,
    *,
    delivery_tier: str | None = None,
    tracker_active: bool = False,
    tracker_closed: bool = False,
    setup_id: str = "",
    state: Any | None = None,
) -> DeliveryFsmState | None:
    """Advance FSM and append funnel telemetry when stage changes."""
    fsm = advance_delivery_fsm(
        symbol,
        direction,
        setup,
        delivery_tier=delivery_tier,
        tracker_active=tracker_active,
        tracker_closed=tracker_closed,
        setup_id=setup_id,
        state=state,
    )
    if fsm is None or not fsm.transitioned:
        return fsm
    from hunt_core.track.events import record_phase_transition

    record_phase_transition(
        symbol=symbol,
        direction=direction,
        from_phase=fsm.previous.value if fsm.previous else "",
        to_phase=fsm.stage.value,
        detail=f"delivery_fsm:{fsm.setup_id}",
        payload={
            "fsm": "delivery",
            "setup_id": fsm.setup_id,
            "delivery_tier": delivery_tier,
        },
    )
    return fsm


# Mid-file __all__ removed (P4) — full public API exported via detect/lifecycle shim.

# --- lifecycle sticky debounce (merged from lifecycle_sticky.py) ---
# Cross-bucket flips (long_leg → dump) need more evidence than cosmetic renames.
_TICKS_CROSS_BUCKET = 3
_TICKS_SAME_BUCKET = 2
_TICKS_LONG_TO_DUMP = 4


def _phase_bucket(phase: str) -> str:
    if phase in {
        HuntPhase.ACCUMULATION.value,
        HuntPhase.IMPULSE_INITIATING.value,
        HuntPhase.BREAKOUT_ARMING.value,
        HuntPhase.POST_DUMP_BOUNCE.value,
        HuntPhase.RECOVERY.value,
    }:
        return "long_leg"
    if phase in {
        HuntPhase.EXHAUSTION_AT_HIGH.value,
        HuntPhase.DISTRIBUTION.value,
        HuntPhase.DUMP_INITIATING.value,
    }:
        return "short_fade"
    if phase == HuntPhase.DUMP_ACTIVE.value:
        return "dump"
    return "other"


@dataclass(slots=True)
class _StickyEntry:
    phase: str
    bias: str
    pending_phase: str | None = None
    pending_count: int = 0
    dump_entered_at: float | None = None
    last_fall_pct: float | None = None
    fall_stable_since: float | None = None


_BOUNCE_PHASES = frozenset(
    {
        HuntPhase.POST_DUMP_BOUNCE.value,
        HuntPhase.RECOVERY.value,
        HuntPhase.ACCUMULATION.value,
    }
)


def _latched_invalidate_short(ph: HuntPhase, raw: HuntLifecycle) -> bool:
    """Honor bounce invalidation even while sticky holds dump_active."""
    return raw.invalidate_short


def _rebuild(raw: HuntLifecycle, *, phase: str, bias: str) -> HuntLifecycle:
    """Keep fresh metrics from raw tick; latch phase/bias flags to sticky values."""
    try:
        ph = HuntPhase(phase)
    except ValueError:
        ph = raw.phase

    inv_short = _latched_invalidate_short(ph, raw)
    short_entry_ok = ph in {
        HuntPhase.EXHAUSTION_AT_HIGH,
        HuntPhase.DISTRIBUTION,
        HuntPhase.DUMP_INITIATING,
    } and not inv_short
    short_confirm_ok = ph in {
        HuntPhase.EXHAUSTION_AT_HIGH,
        HuntPhase.DISTRIBUTION,
        HuntPhase.DUMP_INITIATING,
        HuntPhase.DUMP_ACTIVE,
    } and not inv_short
    if ph == HuntPhase.DUMP_ACTIVE:
        short_entry_ok = False

    return HuntLifecycle(
        phase=ph,
        recommended_bias=bias,  # type: ignore[arg-type]
        short_entry_ok=short_entry_ok,
        short_confirm_ok=short_confirm_ok,
        invalidate_short=inv_short,
        fall_from_high_pct=raw.fall_from_high_pct,
        bounce_from_low_pct=raw.bounce_from_low_pct,
        local_support=raw.local_support,
        local_resistance=raw.local_resistance,
        reasons=(*raw.reasons, f"sticky_hold={phase}"),
        phase_4h=raw.phase_4h,
    )


def _update_dump_sticky_tracking(entry: _StickyEntry, raw: HuntLifecycle, *, now: float) -> None:
    """Track dump_active dwell + fall plateau for faster dump→accumulation debounce."""
    if entry.phase == HuntPhase.DUMP_ACTIVE.value:
        if entry.dump_entered_at is None:
            entry.dump_entered_at = now
        fall = raw.fall_from_high_pct
        if entry.last_fall_pct is not None and fall > entry.last_fall_pct + _FALL_STABLE_EPS_PCT:
            entry.fall_stable_since = None
        elif entry.fall_stable_since is None:
            entry.fall_stable_since = now
        entry.last_fall_pct = fall
        return
    entry.dump_entered_at = None
    entry.last_fall_pct = None
    entry.fall_stable_since = None


def _dump_stabilized(entry: _StickyEntry, *, now: float) -> bool:
    if entry.fall_stable_since is None:
        return False
    return (now - entry.fall_stable_since) >= _DUMP_ACTIVE_MAX_DURATION_S


def stabilize(
    symbol: str,
    raw: HuntLifecycle,
    *,
    state: Any | None = None,
) -> HuntLifecycle:
    """Return debounced lifecycle for watch tick."""
    store = _resolve_state(state)
    sym = symbol.upper()
    raw_phase = raw.phase.value
    raw_bias = str(raw.recommended_bias or "wait")
    now = time.monotonic()

    entry = store.sticky.get(sym)
    if entry is None:
        store.sticky[sym] = _StickyEntry(raw_phase, raw_bias)
        _update_dump_sticky_tracking(store.sticky[sym], raw, now=now)
        return raw

    _update_dump_sticky_tracking(entry, raw, now=now)

    if raw_phase == entry.phase:
        entry.pending_phase = None
        entry.pending_count = 0
        entry.bias = raw_bias
        return raw

    cur_bucket = _phase_bucket(entry.phase)
    new_bucket = _phase_bucket(raw_phase)
    need = _TICKS_SAME_BUCKET if cur_bucket == new_bucket else _TICKS_CROSS_BUCKET
    if cur_bucket == "long_leg" and new_bucket == "dump":
        need = 3  # default long→dump debounce (was 4 — too slow for 5–8% memecoin dumps)
        if raw.fall_from_high_pct >= 15.0:
            need = 1
        elif raw.fall_from_high_pct >= 5.0:
            need = 2
    elif cur_bucket == "dump" and new_bucket == "long_leg":
        guard_fired = any("dump_active_stabilized" in r for r in raw.reasons)
        if guard_fired or _dump_stabilized(entry, now=now):
            need = 1

    if entry.pending_phase != raw_phase:
        entry.pending_phase = raw_phase
        entry.pending_count = 1
    else:
        entry.pending_count += 1

    if entry.pending_count >= need:
        entry.phase = raw_phase
        entry.bias = raw_bias
        entry.pending_phase = None
        entry.pending_count = 0
        return raw

    return _rebuild(raw, phase=entry.phase, bias=entry.bias)


def reset_symbol(symbol: str, *, state: Any | None = None) -> None:
    _resolve_state(state).reset_symbol(symbol)


def sticky_snapshot(*, state: Any | None = None) -> dict[str, Any]:
    store = _resolve_state(state)
    return {
        sym: {
            "phase": e.phase,
            "bias": e.bias,
            "pending": e.pending_phase,
            "pending_n": e.pending_count,
            "dump_entered_at": e.dump_entered_at,
            "fall_stable_since": e.fall_stable_since,
            "last_fall_pct": e.last_fall_pct,
        }
        for sym, e in store.sticky.items()
    }


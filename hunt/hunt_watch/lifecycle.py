"""Memecoin hunt lifecycle — phases from VELVET/BEAT post-mortems.

VELVET short worked: fade at impulse *high* (RSI OB, rejection, cascade down).
BEAT short failed when lifecycle used rally_from_24h_low as "post_dump_bounce" on a
+89% up-leg (+400% multi-day) — that metric is the pump, not recovery after a dump.

post_dump_bounce requires meaningful_dump (fall_from_hunt_high >= 8%). Parabolic legs
(leg_gain >= 20%, fall < 8%) map to distribution / exhaustion_at_high (short bias).

Phases gate Telegram — not delivery path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

WatchBias = Literal["short", "long", "both", "wait"]

HuntPhaseName = Literal[
    "exhaustion_at_high",
    "distribution",
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


def _f(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if v == v else default  # NaN guard
    except TypeError, ValueError:
        return default


def _local_pivot_support(tf: dict[str, Any], *, impulse_low: float, session_low: float) -> float:
    """Recent higher-low proxy — not the impulse leg high (BEAT bug)."""
    candidates: list[float] = []
    for key in ("15m_closed", "5m_closed"):
        block = tf.get(key) or {}
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
        hi = _f((block.get("candle") or {}).get("high"))
        if hi > 0:
            candidates.append(hi)
        prev_hi = _f(block.get("prev_high"))
        if prev_hi > 0:
            candidates.append(prev_hi)
    if not candidates:
        return impulse_high
    return max(candidates)


def assess_hunt_lifecycle(
    *,
    price: float,
    hunt_high: float,
    hunt_low: float,
    session: dict[str, Any],
    tf: dict[str, Any],
    market: dict[str, Any],
    symbol: str = "",
) -> HuntLifecycle:
    """Classify memecoin leg phase for short/long gating."""
    from hunt_watch.param_store import lifecycle_thresholds

    th = lifecycle_thresholds(symbol)
    meaningful_dump_pct = th.get("meaningful_dump_pct", 8.0)
    parabolic_leg_pct = th.get("parabolic_leg_gain_pct", 20.0)
    mega_leg_pct = th.get("mega_leg_gain_pct", 80.0)
    near_high_pos = th.get("near_high_pos", 0.82)
    near_high_ratio = th.get("near_high_price_ratio", 0.97)
    post_dump_pos = th.get("post_dump_bounce_pos", 0.55)
    bounce_floor = th.get("bounce_min_floor_pct", 5.0)
    bounce_atr_mult = th.get("bounce_min_atr_mult", 1.5)
    rsi_ob = th.get("rsi_1h_overbought", 65.0)

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
        )

    sess_hi = _f(session.get("high_24h"), hunt_high)
    sess_lo = _f(session.get("low_24h"), hunt_low)
    pos = _f(session.get("pos_in_range"), 0.5)

    fall_from_high = max(0.0, (hunt_high - price) / hunt_high) * 100.0
    # rally_from_24h_low — NOT "bounce after dump". On BEAT +400% this was +33% while
    # price was still in a parabolic leg, mislabeling post_dump_bounce + long bias.
    rally_from_24h_low = max(0.0, (price - sess_lo) / sess_lo) * 100.0 if sess_lo > 0 else 0.0
    bounce_from_low = rally_from_24h_low  # telemetry alias
    leg_gain_pct = (
        max(0.0, (hunt_high - hunt_low) / hunt_low) * 100.0 if hunt_low > 0 else 0.0
    )
    near_high = price >= hunt_high * near_high_ratio or pos >= near_high_pos
    meaningful_dump = fall_from_high >= meaningful_dump_pct
    mega_parabolic_leg = leg_gain_pct >= mega_leg_pct
    upward_leg_shallow_pullback = leg_gain_pct >= parabolic_leg_pct and fall_from_high < meaningful_dump_pct

    r1h = tf.get("1h") or {}
    r5 = tf.get("5m_closed") or tf.get("5m") or {}
    c5 = (r5.get("candle") or {}) if isinstance(r5.get("candle"), dict) else {}
    taker = market.get("taker_5m") or market.get("taker_1h")
    micro = market.get("microprice_bias")

    # Hysteresis: one 5m memecoin candle is 3%+ — a "bounce" must beat ATR noise
    # (HYPE phase_change x5/hour churn lesson). Threshold = max(5%, 1.5 x ATR1h%).
    atr1h_pct = _f(r1h.get("atr_pct"), 0.0)
    bounce_min = max(bounce_floor, bounce_atr_mult * atr1h_pct)

    local_support = _local_pivot_support(tf, impulse_low=hunt_low, session_low=sess_lo)
    local_resistance = _local_pivot_resistance(tf, impulse_high=hunt_high)

    reasons: list[str] = []
    rsi_1h = _f(r1h.get("rsi14"), 50.0)

    # --- phase detection ---
    taker_buy = taker is not None and taker > 1.05
    taker_sell = taker is not None and taker < 0.98
    bull_cascade = c5.get("bullish") and _f(c5.get("lower_wick_ratio")) >= 0.25
    bear_cascade = c5.get("bearish") and _f(c5.get("upper_wick_ratio")) >= 0.25

    r1h_pctile = r1h.get("bb_width_pctile")
    r1h_don = r1h.get("donchian_width_pct")
    squeeze_charged = (
        r1h_pctile is not None
        and r1h_don is not None
        and float(r1h_pctile) <= 0.25
        and float(r1h_don) <= 10.0
    )

    # Initial pump leg — BEAT Jun6-8 / VELVET Jun7-10: catch BEFORE exhaustion fade zone.
    impulse_rally = (
        leg_gain_pct >= parabolic_leg_pct
        and rally_from_24h_low >= bounce_min
        and not meaningful_dump
        and 0.40 <= pos < 0.75
        and rsi_1h < rsi_ob
        and (taker_buy or bull_cascade or (micro is not None and micro > 0))
    )
    if impulse_rally:
        phase = HuntPhase.IMPULSE_INITIATING
        reasons.append(
            f"impulse_initiating_leg{leg_gain_pct:.0f}%_pos={pos:.2f}_rally{rally_from_24h_low:.1f}%"
        )
    elif (
        squeeze_charged
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
        if near_high and rsi_1h >= rsi_ob:
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
        if near_high and rsi_1h >= rsi_ob:
            phase = HuntPhase.EXHAUSTION_AT_HIGH
            reasons.append(
                f"mega_leg_exhaustion_{leg_gain_pct:.0f}%_fall{fall_from_high:.1f}%"
            )
        elif (taker_buy or bull_cascade or (micro is not None and micro > 0)) and (
            fall_from_high < 12.0 or not near_high
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
    elif meaningful_dump and rally_from_24h_low >= bounce_min and pos >= post_dump_pos:
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
        and fall_from_high < 18.0
        and (taker_buy or bull_cascade)
    ):
        phase = HuntPhase.RECOVERY
        reasons.append(f"squeeze_recovery_pos={pos:.2f}_fall{fall_from_high:.1f}%")
    elif near_high and rsi_1h >= 65:
        phase = HuntPhase.EXHAUSTION_AT_HIGH
        reasons.append(f"near_high_pos={pos:.2f}_rsi1h={rsi_1h:.0f}")
    elif (
        fall_from_high >= meaningful_dump_pct
        and pos < 0.55
        and (taker_sell or bear_cascade)
    ):
        phase = HuntPhase.DUMP_ACTIVE
        reasons.append(f"fall_from_high={fall_from_high:.1f}%")
    elif fall_from_high >= 12.0 and pos < 0.55:
        phase = HuntPhase.DUMP_ACTIVE
        reasons.append(f"mid_dump_fall={fall_from_high:.1f}%_pos={pos:.2f}")
    elif (
        meaningful_dump
        and not mega_parabolic_leg
        and rally_from_24h_low >= 15.0
        and fall_from_high >= 10.0
    ):
        phase = HuntPhase.POST_DUMP_BOUNCE
        reasons.append(
            f"deep_post_dump_bounce_fall{fall_from_high:.1f}%_rally{rally_from_24h_low:.1f}%"
        )
    elif fall_from_high >= 2.0 and fall_from_high < 10.0 and pos >= 0.55 and bear_cascade:
        phase = HuntPhase.DISTRIBUTION
        reasons.append("distribution_break_forming")
    elif pos <= 0.35 and rsi_1h <= 40:
        if fall_from_high >= 12.0:
            phase = HuntPhase.DUMP_ACTIVE
            reasons.append(f"low_pos_mid_dump_fall={fall_from_high:.1f}%")
        else:
            phase = HuntPhase.ACCUMULATION
            reasons.append("post_dump_accumulation")
    elif (
        rally_from_24h_low >= 12.0
        and 0.40 <= pos < 0.80
        and fall_from_high < 12.0
        and rsi_1h < 68
        and (taker_buy or bull_cascade or (micro is not None and micro > 0))
    ):
        phase = HuntPhase.IMPULSE_INITIATING
        reasons.append(f"rally_impulse_{rally_from_24h_low:.1f}%_pos={pos:.2f}")
    else:
        phase = HuntPhase.NO_SETUP
        reasons.append("no_clear_phase")

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
    if bounce_phase and not closed_15m_break_up:
        reasons.append("bounce_unconfirmed_15m")
    short_entry_ok = phase in {HuntPhase.EXHAUSTION_AT_HIGH, HuntPhase.DISTRIBUTION}
    short_confirm_ok = (
        phase
        in {
            HuntPhase.EXHAUSTION_AT_HIGH,
            HuntPhase.DISTRIBUTION,
            HuntPhase.DUMP_ACTIVE,
        }
        and not invalidate_short
    )

    if phase == HuntPhase.DUMP_ACTIVE:
        # VELVET lesson: mid-dump is valid *monitoring* but late TG entry loses (BEAT at 4.65).
        short_entry_ok = False
        reasons.append("no_new_short_entry_mid_dump")

    bias: WatchBias = "wait"
    if phase in {HuntPhase.EXHAUSTION_AT_HIGH, HuntPhase.DISTRIBUTION}:
        bias = "short"
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
    )


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
    from hunt_watch.param_store import effective_hunt_params

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
        return False, [], note
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
    from hunt_watch.param_store import lifecycle_thresholds

    th = lifecycle_thresholds(symbol)
    pos_tight = th.get("premature_exhaustion_pos_tight", 0.92)
    bounce_tight = th.get("premature_exhaustion_bounce_tight_pct", 4.0)
    pos_lo = th.get("premature_exhaustion_pos", 0.85)
    bounce_lo = th.get("premature_exhaustion_bounce_pct", 15.0)

    if phase != HuntPhase.EXHAUSTION_AT_HIGH.value:
        return False, ""
    if has_bear_div or fall_from_high_pct >= 5.0:
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

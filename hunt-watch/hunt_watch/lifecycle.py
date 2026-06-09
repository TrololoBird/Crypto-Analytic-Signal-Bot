"""Memecoin hunt lifecycle — phases from VELVET/BEAT post-mortems.

VELVET short worked: fade at impulse *high* (RSI OB, rejection, cascade down).
BEAT short failed: alert after −8% from high into squeeze bounce (sticky confirm on impulse high).

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
    "no_setup",
]


class HuntPhase(StrEnum):
    EXHAUSTION_AT_HIGH = "exhaustion_at_high"
    DISTRIBUTION = "distribution"
    DUMP_ACTIVE = "dump_active"
    POST_DUMP_BOUNCE = "post_dump_bounce"
    RECOVERY = "recovery"
    ACCUMULATION = "accumulation"
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
    for key in ("15m", "5m"):
        block = tf.get(key) or {}
        lo = _f(block.get("close")) * 0.995 if block.get("close") else 0.0
        if lo > 0:
            candidates.append(lo)
    pivot = max(candidates) if candidates else 0.0
    floor = max(impulse_low, session_low, 0.0)
    if pivot <= floor:
        return floor
    return pivot


def _local_pivot_resistance(tf: dict[str, Any], *, impulse_high: float) -> float:
    candidates: list[float] = [impulse_high]
    for key in ("5m_closed", "15m_closed", "5m", "15m"):
        block = tf.get(key) or {}
        hi = _f((block.get("candle") or {}).get("high"))
        if hi > 0:
            candidates.append(hi)
        close = _f(block.get("close"))
        if close > 0:
            candidates.append(close)
    return max(candidates)


def assess_hunt_lifecycle(
    *,
    price: float,
    hunt_high: float,
    hunt_low: float,
    session: dict[str, Any],
    tf: dict[str, Any],
    market: dict[str, Any],
) -> HuntLifecycle:
    """Classify memecoin leg phase for short/long gating."""
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
    bounce_from_low = max(0.0, (price - sess_lo) / sess_lo) * 100.0 if sess_lo > 0 else 0.0
    near_high = price >= hunt_high * 0.97 or pos >= 0.82

    r1h = tf.get("1h") or {}
    r5 = tf.get("5m_closed") or tf.get("5m") or {}
    c5 = (r5.get("candle") or {}) if isinstance(r5.get("candle"), dict) else {}
    taker = market.get("taker_5m") or market.get("taker_1h")
    micro = market.get("microprice_bias")

    local_support = _local_pivot_support(tf, impulse_low=hunt_low, session_low=sess_lo)
    local_resistance = _local_pivot_resistance(tf, impulse_high=hunt_high)

    reasons: list[str] = []
    rsi_1h = _f(r1h.get("rsi14"), 50.0)

    # --- phase detection (bounce/recovery before distribution — BEAT fix) ---
    taker_buy = taker is not None and taker > 1.05
    taker_sell = taker is not None and taker < 0.98
    bull_cascade = c5.get("bullish") and _f(c5.get("lower_wick_ratio")) >= 0.25
    bear_cascade = c5.get("bearish") and _f(c5.get("upper_wick_ratio")) >= 0.25

    if bounce_from_low >= 3.0 and pos >= 0.60 and fall_from_high >= 3.0:
        if taker_buy or bull_cascade or (micro is not None and micro > 0):
            phase = HuntPhase.POST_DUMP_BOUNCE
            reasons.append(f"bounce={bounce_from_low:.1f}%_pos={pos:.2f}")
            if taker_buy:
                reasons.append(f"taker_buy={taker:.2f}")
        elif pos >= 0.75 and fall_from_high < 12.0:
            phase = HuntPhase.RECOVERY
            reasons.append(f"recovery_pos={pos:.2f}_off_high={fall_from_high:.1f}%")
        else:
            phase = HuntPhase.POST_DUMP_BOUNCE
            reasons.append(f"bounce={bounce_from_low:.1f}%")
    elif (
        pos >= 0.75
        and fall_from_high >= 3.0
        and fall_from_high < 12.0
        and (taker_buy or bull_cascade)
    ):
        phase = HuntPhase.RECOVERY
        reasons.append(f"squeeze_recovery_pos={pos:.2f}")
    elif near_high and rsi_1h >= 65:
        phase = HuntPhase.EXHAUSTION_AT_HIGH
        reasons.append(f"near_high_pos={pos:.2f}_rsi1h={rsi_1h:.0f}")
    elif fall_from_high >= 6.0 and pos < 0.55 and (taker_sell or bear_cascade):
        phase = HuntPhase.DUMP_ACTIVE
        reasons.append(f"fall_from_high={fall_from_high:.1f}%")
    elif fall_from_high >= 2.0 and fall_from_high < 10.0 and pos >= 0.55 and bear_cascade:
        phase = HuntPhase.DISTRIBUTION
        reasons.append("distribution_break_forming")
    elif pos <= 0.35 and rsi_1h <= 40:
        phase = HuntPhase.ACCUMULATION
        reasons.append("post_dump_accumulation")
    else:
        phase = HuntPhase.NO_SETUP
        reasons.append("no_clear_phase")

    invalidate_short = phase in {
        HuntPhase.POST_DUMP_BOUNCE,
        HuntPhase.RECOVERY,
        HuntPhase.ACCUMULATION,
    }
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
    elif phase in {HuntPhase.POST_DUMP_BOUNCE, HuntPhase.ACCUMULATION, HuntPhase.RECOVERY}:
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
) -> float:
    """Support for confirm: local pivot in distribution; impulse high only at exhaustion."""
    if lifecycle.phase == HuntPhase.EXHAUSTION_AT_HIGH:
        return round(impulse_high * 0.998, 6)
    return lifecycle.local_support


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
    # Late entry guard: already fell >10% from high — confirm downgraded unless at local support break
    if lifecycle.fall_from_high_pct >= 10.0 and not lifecycle.short_entry_ok:
        return False, hard, "late_short_after_dump"
    return confirmed, hard, None


def blocks_premature_exhaustion_short(
    *,
    phase: str,
    fall_from_high_pct: float,
    bounce_from_low_pct: float,
    pos_in_range: float,
    has_bear_div: bool,
) -> tuple[bool, str]:
    """Block fade-at-top shorts when only LTF broke impulse-high (JCT lesson)."""
    if phase != HuntPhase.EXHAUSTION_AT_HIGH.value:
        return False, ""
    if has_bear_div or fall_from_high_pct >= 5.0:
        return False, ""
    if pos_in_range > 0.85 and bounce_from_low_pct > 15.0:
        return (
            True,
            f"premature_exhaustion_pos={pos_in_range:.2f}_bounce={bounce_from_low_pct:.1f}%",
        )
    return False, ""

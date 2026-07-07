"""Risk/reward floors, TP2 room, and short dump timing gates."""
from __future__ import annotations

from typing import Any, Literal

from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.regime.market_regime import HuntCalibratedParams
from hunt_core.scanner.gate._types import BOUNCE_MIN_RISK_REWARD, GateResult
from hunt_core.params.store import delivery_thresholds, effective_hunt_params

PUMP_PHASES_LONG = frozenset({"impulse_initiating", "breakout_arming"})
FADE_PHASES_SHORT = frozenset({"exhaustion_at_high", "distribution"})
SHORT_DUMP_START_LC_PHASES = frozenset(
    {"exhaustion_at_high", "distribution", "dump_initiating"}
)
# Deprecated — mid-leg continuation is monitor-only (mission lock).
DUMP_CONTINUATION_PHASES = frozenset()
STRUCTURAL_DUMP_MARKERS = (
    "close_below_support",
    "below_support",
    "live_below_support",
    "lost_support",
    "bear_cascade",
)
STRUCTURAL_DUMP_PHASES = frozenset(
    {
        "dump_initiating",
        "dump_confirmed",
        "dump_setup_forming",
        "dump_imminent",
    }
)
PRE_DUMP_LC_PHASES = frozenset(
    {
        "exhaustion_at_high",
        "distribution",
        "dump_initiating",
    }
)

_DUMP_CONTINUATION_MIN_RR = 1.05
_CONTINUATION_PCT_MIN_RR = 0.85
_CONFIRMED_STRUCTURAL_DUMP_MIN_RR = 1.05
_EXHAUSTION_FADE_DELIVERY_MIN_RR = 2.0
_PRE_DUMP_STRUCTURAL_MIN_RR = 1.15
DELIVERY_MIN_RR_FLOOR = 1.6


def setup_fuel_legacy(setup: dict[str, Any], direction: str) -> float:
    key = "dump_fuel" if direction == "short" else "long_fuel"
    alt = "dump_score" if direction == "short" else "long_score"
    return float(setup.get(key) or setup.get(alt) or 0)


def setup_fuel(setup: dict[str, Any], direction: str) -> float:
    """Delivery strength 0–100 — P(win)×100 when calibrated, else legacy fuel score."""
    from hunt_core.scanner.gate._ev import setup_confidence_score

    p = setup_confidence_score(setup)
    if p is not None:
        return p * 100.0
    return setup_fuel_legacy(setup, direction)


def min_rr(symbol: str, direction: str, lc: dict[str, Any]) -> float:
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    if sym in PINNED_SYMBOLS:
        return cal.pinned_min_risk_reward
    phase = str(lc.get("phase") or "")
    if direction == "long" and phase == "post_dump_bounce":
        return BOUNCE_MIN_RISK_REWARD
    return cal.min_risk_reward


def structural_dump_hard(hard: list[Any]) -> bool:
    return any(
        any(marker in str(h) for marker in STRUCTURAL_DUMP_MARKERS) for h in hard
    )


def structural_hard_count(hard: list[Any], *, direction: str) -> int:
    keys_short = (
        "close_below_support",
        "rejection",
        "cascade",
        "1m_close",
        "5m_close",
        "15m_close",
        "bear_cascade",
        "lost_support",
        "pp_short",
        "dump_continuation_confirm",
        "prokol_reclaim",
        "bos_retest",
        "intrabar_ignition",
    )
    keys_long = (
        "close_above_resistance",
        "bounce",
        "cascade",
        "broke_resistance",
        "5m_close",
        "bull_cascade",
        "ws_taker_buy",
        "prokol_reclaim",
        "bos_retest",
        "intrabar_ignition",
    )
    keys = keys_short if direction == "short" else keys_long
    return sum(1 for h in hard if any(k in str(h) for k in keys))


def short_dump_start_max_fall_pct(symbol: str = "") -> float:
    dl = delivery_thresholds(symbol)
    return float(dl.get("short_start_max_fall_pct", 3.0))


def short_pre_dump_headroom_pct(symbol: str = "") -> float:
    dl = delivery_thresholds(symbol)
    return float(dl.get("short_pre_dump_headroom_pct", 5.0))


def short_dump_first_break_max_fall_pct(symbol: str = "") -> float:
    dl = delivery_thresholds(symbol)
    return float(dl.get("short_first_break_max_fall_pct", 5.0))


def in_pre_dump_window(
    lc: dict[str, Any],
    *,
    symbol: str = "",
    hunt_high: float = 0.0,
    price: float = 0.0,
) -> bool:
    fall = float(lc.get("fall_from_high_pct") or 0)
    start_max = short_dump_start_max_fall_pct(symbol)
    headroom_max = short_pre_dump_headroom_pct(symbol)
    if fall <= start_max:
        return True
    if hunt_high > 0 and price > 0:
        headroom = max(0.0, (hunt_high - price) / hunt_high * 100.0)
        return headroom <= headroom_max and fall <= headroom_max
    return fall <= headroom_max


def close_below_support_in_hard(hard: list[Any]) -> bool:
    return any("close_below_support" in str(h) for h in hard)


def late_dump_depth_chase_block(
    *,
    fall: float,
    pos_in_range: float,
    phase: str,
    hard: list[Any],
) -> GateResult | None:
    if fall <= 15.0 or pos_in_range >= 0.25:
        return None
    if close_below_support_in_hard(hard) and structural_hard_count(hard, direction="short") >= 2:
        return None
    where = phase or "dump"
    return GateResult(
        False,
        "dump_deep_chase",
        f"Fall {fall:.1f}% + pos_in_range {pos_in_range:.2f} ({where}) — "
        "поздний chase у дна диапазона",
    )


def order_flow_demotes_triggered(row: dict[str, Any], *, direction: str) -> bool:
    from hunt_core.toolkit.order_flow import synthesize_order_flow

    market = row.get("market") or {}
    taker = market.get("taker_5m")
    taker_5m = float(taker) if taker is not None else 1.0
    of = synthesize_order_flow(row)
    if direction == "short":
        if taker_5m < 1.0:
            return False
        return of.cvd_trend == "bear" and of.aggressor == "buyers"
    if direction == "long":
        if taker_5m > 1.0:
            return False
        return of.cvd_trend == "bull" and of.aggressor == "sellers"
    return False


def short_dump_delivery_too_late(
    lc: dict[str, Any],
    setup: dict[str, Any],
    *,
    symbol: str = "",
) -> GateResult | None:
    fall = float(lc.get("fall_from_high_pct") or 0)
    phase = str(lc.get("phase") or "")
    setup_phase = str(setup.get("phase") or "")
    if phase == "dump_active":
        return GateResult(
            False,
            "dump_mid_leg",
            f"Дамп уже идёт (fall {fall:.1f}%) — monitor only, без нового TG",
        )
    start_max = short_dump_start_max_fall_pct(symbol)
    break_max = short_dump_first_break_max_fall_pct(symbol)
    headroom_max = short_pre_dump_headroom_pct(symbol)
    if fall <= start_max:
        return None
    if (
        fall <= break_max
        and fall <= headroom_max
        and phase in SHORT_DUMP_START_LC_PHASES
        and setup_phase
        in {
            "dump_initiating",
            "dump_confirmed",
            "dump_setup_forming",
            "dump_imminent",
        }
    ):
        return None
    return GateResult(
        False,
        "dump_late_entry",
        f"Fall {fall:.1f}% > {start_max:.0f}% — пропустили начало, поздний дамп не шлём",
    )


def dump_continuation_short_ok(
    setup: dict[str, Any],
    *,
    phase: str,
    lc: dict[str, Any],
    confidence_score: float | None,
    cal_min_p_win: float,
    pos_in_range: float | None = None,
) -> bool:
    """Deprecated — mid-dump continuation is never valid for watch TG delivery."""
    _ = setup, phase, lc, confidence_score, cal_min_p_win, pos_in_range
    return False


def continuation_pct_min_rr(setup: dict[str, Any]) -> float | None:
    mode = str(setup.get("level_mode") or "")
    if "continuation_pct" in mode:
        return _CONTINUATION_PCT_MIN_RR
    tp2_label = str(setup.get("tp2_label") or "")
    if "cont" in tp2_label.lower():
        return _CONTINUATION_PCT_MIN_RR
    return None


def confirmed_structural_dump_min_rr(
    setup: dict[str, Any],
    lc: dict[str, Any],
) -> float | None:
    if not bool(setup.get("impulse_confirmed")):
        return None
    hard = setup.get("confirm_hard") or []
    if not structural_dump_hard(hard) and structural_hard_count(hard, direction="short") < 1:
        return None
    phase = str(lc.get("phase") or "")
    if phase in PRE_DUMP_LC_PHASES:
        return _PRE_DUMP_STRUCTURAL_MIN_RR
    if phase not in STRUCTURAL_DUMP_PHASES and phase not in DUMP_CONTINUATION_PHASES:
        return None
    return _CONFIRMED_STRUCTURAL_DUMP_MIN_RR


def effective_min_rr(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lc: dict[str, Any],
    cal: HuntCalibratedParams,
) -> float:
    base = min_rr(symbol, direction, lc)
    phase = str(lc.get("phase") or "")
    # Pre-pump/pre-dump entries sit at accumulation zone; nearest resistance may give R:R ~1.1-1.3 —
    # enforcing 1.6 kills every such setup before price has confirmed the move.
    # Pre-pump entry sits at accumulation zone; TP1 is nearest resistance and may give R:R ~1.1.
    # The real target is TP2/TP3; a 1:1 floor is the minimum meaningful gate here.
    if direction == "long" and phase in {"pre_pump", "accumulation", "breakout_arming"}:
        return 1.0
    if direction != "short":
        return max(base, DELIVERY_MIN_RR_FLOOR)
    if phase in {"pre_dump", "distribution"}:
        return max(base, _PRE_DUMP_STRUCTURAL_MIN_RR)
    if phase == "exhaustion_at_high":
        return max(base, _EXHAUSTION_FADE_DELIVERY_MIN_RR)
    cont_floor = continuation_pct_min_rr(setup)
    if cont_floor is not None:
        return min(base, cont_floor)
    structural_floor = confirmed_structural_dump_min_rr(setup, lc)
    if structural_floor is not None:
        return min(base, structural_floor)
    if bool(setup.get("impulse_confirmed")):
        return max(base, _CONFIRMED_STRUCTURAL_DUMP_MIN_RR)
    return max(base, DELIVERY_MIN_RR_FLOOR)


def effective_min_rr_for_delivery(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: dict[str, Any] | None = None,
) -> float:
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    return effective_min_rr(
        setup,
        direction=direction,
        symbol=sym,
        lc=lc,
        cal=cal,
    )


def tp2_room_blocks(
    setup: dict[str, Any],
    *,
    price: float,
    min_room_pct: float,
    min_rr: float,
) -> bool:
    tp2 = float(setup.get("tp2") or 0)
    if price <= 0 or tp2 <= 0:
        return False
    room = abs(price - tp2) / price * 100.0
    if room >= min_room_pct:
        return False
    rr = setup.get("risk_reward")
    if rr is not None and float(rr) >= min_rr:
        return False
    if structural_dump_hard(setup.get("confirm_hard") or []) and len(
        setup.get("confirm_hard") or []
    ) >= 2:
        return False
    return True


# Back-compat private aliases for delivery.py re-exports
_setup_fuel = setup_fuel
_min_rr = min_rr
_structural_dump_hard = structural_dump_hard
_structural_hard_count = structural_hard_count
_short_dump_start_max_fall_pct = short_dump_start_max_fall_pct
_short_pre_dump_headroom_pct = short_pre_dump_headroom_pct
_short_dump_first_break_max_fall_pct = short_dump_first_break_max_fall_pct
_in_pre_dump_window = in_pre_dump_window
_close_below_support_in_hard = close_below_support_in_hard
_late_dump_depth_chase_block = late_dump_depth_chase_block
_short_dump_delivery_too_late = short_dump_delivery_too_late
_effective_min_rr = effective_min_rr
_tp2_room_blocks = tp2_room_blocks
_DELIVERY_MIN_RR_FLOOR = DELIVERY_MIN_RR_FLOOR
_PUMP_PHASES_LONG = PUMP_PHASES_LONG
_FADE_PHASES_SHORT = FADE_PHASES_SHORT
_SHORT_DUMP_START_LC_PHASES = SHORT_DUMP_START_LC_PHASES
_DUMP_CONTINUATION_PHASES = DUMP_CONTINUATION_PHASES

_STRUCTURE_PHASE_SHORT = frozenset(
    {
        "distribution",
        "exhaustion_at_high",
        "dump_initiating",
        "dump_imminent",
        "dump_setup_forming",
        "pre_dump",
    }
)
_STRUCTURE_PHASE_LONG = frozenset(PUMP_PHASES_LONG) | frozenset(
    {"pre_pump", "accumulation", "breakout_arming", "impulse_initiating"}
)


def structure_ev_fuel_cap(
    setup: dict[str, Any],
    *,
    direction: Literal["short", "long"],
    lifecycle_phase: str = "",
) -> float:
    """Max display fuel from tradable geometry (RR, levels, lifecycle phase)."""
    if setup.get("levels_viable") is False:
        return 44.0
    from hunt_core.contract import compute_setup_risk_reward

    rr = compute_setup_risk_reward(setup, direction=direction)
    if rr is None:
        return 52.0
    rr_f = float(rr)
    if rr_f < 0.85:
        return 32.0
    if rr_f < 1.0:
        return 40.0
    if rr_f < 1.25:
        return 50.0
    if rr_f < 1.5:
        return 62.0
    if rr_f < 1.8:
        return 75.0
    phase = lifecycle_phase or str(setup.get("lifecycle_phase") or setup.get("phase") or "")
    allowed = _STRUCTURE_PHASE_SHORT if direction == "short" else _STRUCTURE_PHASE_LONG
    if phase and phase not in allowed:
        return 68.0
    return 100.0


def apply_structure_ev_fuel_cap(
    fuel: float,
    setup: dict[str, Any],
    *,
    direction: Literal["short", "long"],
    lifecycle_phase: str = "",
) -> float:
    cap = structure_ev_fuel_cap(
        setup,
        direction=direction,
        lifecycle_phase=lifecycle_phase,
    )
    capped = round(min(float(fuel), cap), 1)
    if capped < float(fuel) - 0.5:
        setup["structure_ev_cap"] = cap
        setup["fuel_before_structure_cap"] = float(fuel)
    return capped


__all__ = [
    "DELIVERY_MIN_RR_FLOOR",
    "DUMP_CONTINUATION_PHASES",
    "FADE_PHASES_SHORT",
    "PUMP_PHASES_LONG",
    "SHORT_DUMP_START_LC_PHASES",
    "dump_continuation_short_ok",
    "effective_min_rr",
    "effective_min_rr_for_delivery",
    "late_dump_depth_chase_block",
    "min_rr",
    "order_flow_demotes_triggered",
    "setup_fuel",
    "short_dump_delivery_too_late",
    "apply_structure_ev_fuel_cap",
    "structural_dump_hard",
    "structural_hard_count",
    "structure_ev_fuel_cap",
    "tp2_room_blocks",
]

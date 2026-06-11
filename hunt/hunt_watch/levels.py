"""Structural entry / SL / TP for hunt watch (swing + fib, not naive ATR-only).

hunt-v4 risk math (FARTCOIN/INJ/4USDT post-mortem):
- SL anchors to the LOCAL pivot, never the impulse high;
- hard ceiling: SL distance <= 0.5 x TP2 distance — if the noise floor
  (SL_MIN_ATR) does not fit under that ceiling the setup is NOT viable,
  instead of silently widening the stop past the ceiling;
- R:R is measured from the WORST edge of the entry zone, not from mid;
- entry zone width is capped (1.5 ATR / 3%) — a 12%-wide zone is not a zone;
- ATR must be real market data: missing ATR vetoes the setup, no fallback.
"""

from __future__ import annotations

from typing import Any

from hunt_watch.level_calibration import AdaptiveLevelParams, adaptive_level_params

# SL may use at most this fraction of the TP2 distance (worst-edge based).
SL_TP2_CAP_RATIO = 0.5
# Minimum SL breathing room in ATRs so the cap cannot put SL inside noise.
SL_MIN_ATR = 0.6
# Maximum SL distance in ATRs regardless of structure.
SL_MAX_ATR = 2.5
# Entry zone width cap: min(ENTRY_ZONE_MAX_ATR x ATR, ENTRY_ZONE_MAX_PCT of price).
ENTRY_ZONE_MAX_ATR = 1.5
ENTRY_ZONE_MAX_PCT = 3.0
# Nominal risk sanity: SL further than this from the worst edge is untradable.
SL_MAX_PCT = 8.0
# Minimum R:R from the worst edge for a setup to be viable.
MIN_RR = 1.0
_BOUNCE_MIN_RR = 0.5
_PUMP_START_MIN_RR = 0.85


def _phase_min_rr_long(lifecycle_phase: str) -> float:
    p = str(lifecycle_phase or "").strip()
    if p in {"post_dump_bounce", "recovery"}:
        return _BOUNCE_MIN_RR
    if p in {"impulse_initiating", "breakout_arming"}:
        return _PUMP_START_MIN_RR
    return MIN_RR


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _veto(reasons: list[str], price: float) -> dict[str, Any]:
    return {
        "viable": False,
        "veto": reasons,
        "entry_zone": [price, price],
        "stop_loss": 0.0,
        "tp1": 0.0,
        "tp2": 0.0,
        "invalidation_above": 0.0,
        "invalidation_below": 0.0,
        "risk_reward": 0.0,
        "sl_dist_pct": None,
        "tp2_dist_pct": None,
    }


def structural_short_levels(
    *,
    price: float,
    impulse_high: float,
    impulse_low: float,
    fib: dict[str, float],
    atr15: float,
    local_support: float,
    local_resistance: float,
    range_pct_24h: float = 0.0,
    leg_gain_pct: float = 0.0,
    fall_from_high_pct: float = 0.0,
    symbol: str = "",
    lifecycle_phase: str = "",
) -> dict[str, float | list[float] | bool | list[str]]:
    """Short fade: SL above LOCAL pivot, TPs toward impulse low / fib retrace."""
    veto: list[str] = []
    if price <= 0:
        return _veto(["price_missing"], 0.0)
    atr = _f(atr15)
    if atr <= 0:
        return _veto(["atr_missing"], price)
    adapt: AdaptiveLevelParams = adaptive_level_params(
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        fall_from_high_pct=fall_from_high_pct,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase,
    )
    sl_max_pct = adapt.sl_max_pct
    sl_max_atr = adapt.sl_max_atr
    sl_tp2_cap = adapt.sl_tp2_cap_ratio
    if adapt.use_local_pivot_only and local_resistance > 0:
        ih = max(price, local_resistance)
    else:
        ih = max(impulse_high, price, local_resistance)
    il = min(impulse_low, local_support, price) if impulse_low > 0 else local_support

    # Entry anchors to current price; zone width hard-capped — a wide zone means
    # "somewhere around here", which is not an entry.
    entry_hi = round(max(price, min(ih * 0.995, price + atr * 2.0)), 6)
    entry_lo = round(min(price * 0.998, local_support * 1.002, entry_hi * 0.996), 6)
    width_cap = min(atr * ENTRY_ZONE_MAX_ATR, price * ENTRY_ZONE_MAX_PCT / 100.0)
    if entry_hi - entry_lo > width_cap:
        entry_lo = round(entry_hi - width_cap, 6)
    worst = entry_hi  # short fills at the top of the zone in the worst case

    # --- TPs first: the SL ceiling depends on the TP2 distance ---
    tp1 = _f(fib.get("ret_382"))
    tp2 = _f(fib.get("ret_50"))
    if tp1 <= 0 or tp1 >= entry_lo:
        leg = ih - il
        tp1 = round(ih - leg * 0.382, 6) if leg > 0 else round(entry_lo - atr * 2, 6)
    if tp2 <= 0 or tp2 >= tp1:
        leg = ih - il
        tp2 = round(ih - leg * 0.5, 6) if leg > 0 else round(il * 1.01, 6)
    if tp1 >= entry_lo:
        tp1 = round((entry_lo + il) / 2.0, 6)
    if tp2 >= tp1:
        tp2 = round(il * 1.015, 6)

    # --- SL: local pivot anchor + TP2-proportional ceiling, measured from worst edge ---
    pivot = local_resistance if 0 < local_resistance < ih else ih
    stop = max(pivot * 1.004, entry_hi + atr * 1.1)
    stop = min(stop, entry_hi + atr * sl_max_atr)
    floor_stop = entry_hi + atr * SL_MIN_ATR
    tp2_dist = worst - tp2
    cap_stop = worst + tp2_dist * sl_tp2_cap if tp2_dist > 0 else floor_stop
    if floor_stop > cap_stop:
        # The minimum breathing room already breaks the R:R mandate — zone too noisy.
        veto.append("sl_floor_exceeds_tp2_cap")
    stop = round(min(max(stop, floor_stop), max(cap_stop, floor_stop)), 6)

    risk = max(stop - worst, atr * 0.25)
    reward = max(worst - tp1, 0.0)
    rr = round(reward / risk, 2) if risk > 0 else 0.0
    sl_dist_pct = round((stop - worst) / worst * 100.0, 2)
    if sl_dist_pct > sl_max_pct:
        veto.append("sl_nominal_too_wide")
    if rr < MIN_RR:
        veto.append("rr_below_min")

    return {
        "viable": not veto,
        "veto": veto,
        "entry_zone": [entry_lo, entry_hi],
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "invalidation_above": stop,
        "risk_reward": rr,
        "sl_dist_pct": sl_dist_pct,
        "tp2_dist_pct": round((worst - tp2) / worst * 100.0, 2),
        "level_mode": adapt.mode,
        "sl_max_pct_used": sl_max_pct,
    }


def structural_long_levels(
    *,
    price: float,
    impulse_high: float,
    impulse_low: float,
    fib: dict[str, float],
    atr15: float,
    local_support: float,
    local_resistance: float,
    range_pct_24h: float = 0.0,
    leg_gain_pct: float = 0.0,
    fall_from_high_pct: float = 0.0,
    symbol: str = "",
    lifecycle_phase: str = "",
) -> dict[str, float | list[float] | bool | list[str]]:
    """Long bounce: SL under LOCAL pivot support, TPs toward resistance / fib ext."""
    veto: list[str] = []
    if price <= 0:
        return _veto(["price_missing"], 0.0)
    atr = _f(atr15)
    if atr <= 0:
        return _veto(["atr_missing"], price)
    adapt: AdaptiveLevelParams = adaptive_level_params(
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        fall_from_high_pct=fall_from_high_pct,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase,
    )
    sl_max_pct = adapt.sl_max_pct
    sl_max_atr = adapt.sl_max_atr
    sl_tp2_cap = adapt.sl_tp2_cap_ratio
    ih = max(impulse_high, local_resistance, price)
    il = min(impulse_low, local_support, price) if impulse_low > 0 else local_support

    support_zone = _f(fib.get("ret_382"), il)
    entry_lo = round(max(min(price * 0.998, support_zone * 1.002), price - atr * 2.0), 6)
    entry_hi = round(max(price, entry_lo * 1.006), 6)
    width_cap = min(atr * ENTRY_ZONE_MAX_ATR, price * ENTRY_ZONE_MAX_PCT / 100.0)
    if entry_hi - entry_lo > width_cap:
        entry_lo = round(entry_hi - width_cap, 6)
    worst = entry_lo  # long fills at the bottom of the zone in the worst case

    # --- TPs first ---
    tp1 = round(min(local_resistance, ih * 0.998), 6) if local_resistance > 0 else round(ih * 0.998, 6)
    tp2 = _f(fib.get("ext_1272"))
    if tp2 <= tp1:
        leg = ih - il
        tp2 = round(ih + leg * 0.272, 6) if leg > 0 else round(ih * 1.03, 6)
    # Squeeze at/above impulse high — TPs must be above price (STG/EPIC lesson).
    if price >= ih * 0.97:
        tp1 = round(max(tp1, price + atr * 1.8), 6)
        tp2 = round(max(tp2, price + atr * 3.5, ih * 1.02), 6)
    elif tp1 <= entry_hi:
        tp1 = round(entry_hi + atr * 1.5, 6)

    # --- SL: local pivot support anchor + TP2-proportional ceiling, worst-edge based ---
    pivot = local_support if 0 < local_support < price else il
    stop = min(pivot * 0.996, entry_lo - atr * 1.1)
    stop = max(stop, entry_lo - atr * sl_max_atr)
    floor_stop = entry_lo - atr * SL_MIN_ATR
    tp2_dist = tp2 - worst
    cap_stop = worst - tp2_dist * sl_tp2_cap if tp2_dist > 0 else floor_stop
    if floor_stop < cap_stop:
        veto.append("sl_floor_exceeds_tp2_cap")
    stop = round(max(min(stop, floor_stop), min(cap_stop, floor_stop)), 6)
    if stop <= 0:
        veto.append("sl_non_positive")

    risk = max(worst - stop, atr * 0.25)
    reward = max(tp1 - worst, 0.0)
    rr = round(reward / risk, 2) if risk > 0 else 0.0
    sl_dist_pct = round((worst - stop) / worst * 100.0, 2)
    min_rr = _phase_min_rr_long(lifecycle_phase)
    if sl_dist_pct > sl_max_pct:
        veto.append("sl_nominal_too_wide")
    if rr < min_rr:
        veto.append("rr_below_min")

    return {
        "viable": not veto,
        "veto": veto,
        "entry_zone": [entry_lo, entry_hi],
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "invalidation_below": stop,
        "risk_reward": rr,
        "sl_dist_pct": sl_dist_pct,
        "tp2_dist_pct": round((tp2 - worst) / worst * 100.0, 2),
        "level_mode": adapt.mode,
        "sl_max_pct_used": sl_max_pct,
    }


def fib_retracement_levels(high: float, low: float) -> dict[str, float]:
    """Fib extensions above high and retracements into the leg (hunt impulse window)."""
    leg = high - low
    return {
        "ext_1272": round(high + leg * 0.272, 6),
        "ext_1618": round(high + leg * 0.618, 6),
        "ret_236": round(high - leg * 0.236, 6),
        "ret_382": round(high - leg * 0.382, 6),
        "ret_50": round(high - leg * 0.5, 6),
    }

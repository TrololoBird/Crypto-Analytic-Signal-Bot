"""Deep trade plan — single geometry authority (R3)."""
from __future__ import annotations

from typing import Any, Literal

from hunt_core.shared.primitives import atr_pad, forecast_band

_MAX_RR = 10.0
_MIN_RR = 0.3
PlanDirection = Literal["long", "short"]


def _rr(entry: float, target: float, stop: float, direction: str) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    reward = (target - entry) if direction == "long" else (entry - target)
    return reward / risk


def _worst_zone_edge(zone: tuple[float, float], direction: str) -> float:
    lo, hi = zone
    return lo if direction == "long" else hi


def _zone_midpoint(zone: tuple[float, float]) -> float:
    lo, hi = zone
    return (lo + hi) / 2.0


def plan_geometry_valid(
    plan: dict[str, Any],
    *,
    direction: PlanDirection,
) -> bool:
    """True when entry zone and TP1 do not contradict (long: tp1 > zone top)."""
    ez = plan.get("entry_zone") or [0.0, 0.0]
    if len(ez) < 2:
        return False
    lo, hi = float(ez[0]), float(ez[1])
    tp1 = float(plan.get("tp1") or 0)
    if tp1 <= 0 or lo <= 0 or hi <= 0 or lo >= hi:
        return False
    if direction == "long":
        return tp1 > hi
    return tp1 < lo


def _clamp_entry_zone_to_targets(
    ez_lo: float,
    ez_hi: float,
    tps: list[float],
    *,
    direction: PlanDirection,
    atr: float,
    price_hint: float = 0.0,
) -> tuple[float, float]:
    """Ensure zone width sane and zone top/bottom does not cross TP1."""
    if ez_lo >= ez_hi:
        return ez_lo, ez_hi
    ref = price_hint if price_hint > 0 else _zone_midpoint((ez_lo, ez_hi))
    max_width = max(atr * 1.8, ref * 0.018)
    if ez_hi - ez_lo > max_width:
        if direction == "long":
            ez_hi = ez_lo + max_width
        else:
            ez_lo = ez_hi - max_width
    if tps:
        tp1 = tps[0]
        if direction == "long" and tp1 > 0 and ez_hi >= tp1:
            ez_hi = min(ez_hi, tp1 * 0.998)
            if ez_hi <= ez_lo:
                ez_hi = ez_lo + min(max_width, max(atr * 0.35, ref * 0.001))
        elif direction == "short" and tp1 > 0 and ez_lo <= tp1:
            ez_lo = max(ez_lo, tp1 * 1.002)
            if ez_lo >= ez_hi:
                ez_lo = ez_hi - min(max_width, max(atr * 0.35, ref * 0.001))
    return round(ez_lo, 6), round(ez_hi, 6)


def finalize_plan_geometry(
    plan: dict[str, Any],
    *,
    direction: PlanDirection,
    atr: float,
) -> dict[str, Any]:
    """Final normalization — nearest→farthest TPs, monotonic R, SL outside zone."""
    p = dict(plan)
    ez = p.get("entry_zone") or [0.0, 0.0]
    ez_lo = float(ez[0]) if len(ez) >= 2 else 0.0
    ez_hi = float(ez[1]) if len(ez) >= 2 else 0.0
    entry_ref_worst = _worst_zone_edge((ez_lo, ez_hi), direction)
    entry_ref_mid = _zone_midpoint((ez_lo, ez_hi))
    sl = float(p.get("stop_loss") or p.get("stop") or 0.0)

    if direction == "long" and sl >= ez_lo and ez_lo > 0:
        sl = ez_lo - max(atr * 1.2, abs(ez_hi - ez_lo) * 0.5)
        p["stop_loss"] = round(sl, 6)
    elif direction == "short" and sl <= ez_hi and ez_hi > 0:
        sl = ez_hi + max(atr * 1.2, abs(ez_hi - ez_lo) * 0.5)
        p["stop_loss"] = round(sl, 6)

    tps_raw = [p.get("tp1"), p.get("tp2"), p.get("tp3")]
    tps: list[float] = []
    for v in tps_raw:
        if v is None:
            continue
        try:
            f = float(v)
            if direction == "long" and f > entry_ref_mid * 1.0001:
                tps.append(f)
            elif direction == "short" and f < entry_ref_mid * 0.9999:
                tps.append(f)
        except (TypeError, ValueError):
            pass

    if direction == "long":
        tps = sorted(tps)
    else:
        tps = sorted(tps, reverse=True)

    while len(tps) < 3:
        mult = (len(tps) + 2) * 1.5
        if direction == "long":
            tps.append(entry_ref_mid + atr * mult)
        else:
            tps.append(entry_ref_mid - atr * mult)

    for i, tp in enumerate(tps):
        rr = _rr(entry_ref_mid, tp, sl, direction)
        if rr < _MIN_RR or rr > _MAX_RR:
            mult = (i + 2) * 1.5
            tps[i] = (entry_ref_mid + atr * mult) if direction == "long" else (entry_ref_mid - atr * mult)

    if direction == "long":
        tps = sorted(tps)
    else:
        tps = sorted(tps, reverse=True)

    prev_rr = 0.0
    for i, tp in enumerate(tps):
        rr = _rr(entry_ref_mid, tp, sl, direction)
        if rr < prev_rr:
            mult = (i + 2) * 1.5
            tps[i] = (entry_ref_mid + atr * mult) if direction == "long" else (entry_ref_mid - atr * mult)
        prev_rr = max(prev_rr, _rr(entry_ref_mid, tps[i], sl, direction))

    price_hint = float(p.get("price_hint") or entry_ref_mid or 0.0)
    ez_lo, ez_hi = _clamp_entry_zone_to_targets(
        ez_lo, ez_hi, tps, direction=direction, atr=atr, price_hint=price_hint
    )
    entry_ref_worst = _worst_zone_edge((ez_lo, ez_hi), direction)
    entry_ref_mid = _zone_midpoint((ez_lo, ez_hi))
    p["entry_zone"] = [ez_lo, ez_hi]

    p["tp1"] = round(tps[0], 6)
    p["tp2"] = round(tps[1], 6)
    p["tp3"] = round(tps[2], 6)
    p["entry_reference"] = round(entry_ref_mid, 6)
    p["entry_reference_conservative"] = round(entry_ref_worst, 6)
    p["rr_tp1"] = round(_rr(entry_ref_mid, tps[0], sl, direction), 2)
    p["rr_tp2"] = round(_rr(entry_ref_mid, tps[1], sl, direction), 2)
    p["rr_tp3"] = round(_rr(entry_ref_mid, tps[2], sl, direction), 2)
    p["rr_conservative_tp1"] = round(_rr(entry_ref_worst, tps[0], sl, direction), 2)
    p["rr_base_label"] = "≈R:R (от середины зоны)"
    p["geometry_valid"] = plan_geometry_valid(p, direction=direction)
    return p


def validate_plan_geometry(
    plan: dict[str, Any],
    *,
    direction: str,
    atr: float,
) -> dict[str, Any]:
    """Alias — prefer ``finalize_plan_geometry``."""
    return finalize_plan_geometry(plan, direction=direction, atr=atr)  # type: ignore[arg-type]


def build_trade_plan(row: dict[str, Any], *, side: str, price: float, atr: float) -> dict[str, Any]:
    lo, hi = atr_pad(price, atr, k=0.35)
    tgt_lo, tgt_hi = forecast_band(price, atr, side=side, k=1.5)
    stop = price - atr * 1.2 if side == "long" else price + atr * 1.2
    plan = {
        "entry_zone": [round(lo, 6), round(hi, 6)],
        "stop_loss": round(stop, 6),
        "tp1": round(tgt_lo if side == "long" else tgt_hi, 6),
        "tp2": round(tgt_hi if side == "long" else tgt_lo, 6),
        "side": side,
    }
    return finalize_plan_geometry(plan, direction=side, atr=atr)  # type: ignore[arg-type]


__all__ = ["build_trade_plan", "finalize_plan_geometry", "plan_geometry_valid", "validate_plan_geometry"]

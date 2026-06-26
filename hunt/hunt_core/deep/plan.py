"""Deep trade plan — single geometry authority (R3)."""
from __future__ import annotations

from typing import Any, Literal

import structlog

_LOG = structlog.get_logger(__name__)

_MAX_RR = 10.0
_MIN_RR = 0.3
_ZONE_MAX_WIDTH_ATR_MULT = 1.8
_ZONE_MAX_WIDTH_PRICE_PCT = 0.018
_ENTRY_PAD_K = 0.35
PlanDirection = Literal["long", "short"]


def _rr(entry: float, target: float, stop: float, direction: str) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    reward = (target - entry) if direction == "long" else (entry - target)
    return reward / risk


def _worst_zone_edge(zone: tuple[float, float], direction: str) -> float:
    """Worst-case entry edge: long buys at top (hi), short sells at bottom (lo)."""
    lo, hi = zone
    return hi if direction == "long" else lo


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
    max_width = max(atr * _ZONE_MAX_WIDTH_ATR_MULT, ref * _ZONE_MAX_WIDTH_PRICE_PCT)
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
                ez_hi = ez_lo + min(max_width, max(atr * _ENTRY_PAD_K, ref * 0.001))
        elif direction == "short" and tp1 > 0 and ez_lo <= tp1:
            ez_lo = max(ez_lo, tp1 * 1.002)
            if ez_lo >= ez_hi:
                ez_lo = ez_hi - min(max_width, max(atr * _ENTRY_PAD_K, ref * 0.001))
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
        p["geometry_valid"] = False
        return p
    elif direction == "short" and sl <= ez_hi and ez_hi > 0:
        p["geometry_valid"] = False
        return p

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

    if not tps:
        p["geometry_valid"] = False
        return p

    if direction == "long":
        tps = sorted(tps)
    else:
        tps = sorted(tps, reverse=True)

    # Filter TPs with invalid RR — drop instead of replacing with ATR
    tps = [tp for tp in tps if _MIN_RR <= _rr(entry_ref_mid, tp, sl, direction) <= _MAX_RR]
    if not tps:
        p["geometry_valid"] = False
        return p

    if direction == "long":
        tps = sorted(tps)
    else:
        tps = sorted(tps, reverse=True)

    # Deduplicate TPs too close together (0.15% apart) — keep first
    _deduped_tps: list[float] = [tps[0]]
    for tp in tps[1:]:
        if abs(tp - _deduped_tps[-1]) / max(abs(_deduped_tps[-1]), 1e-8) >= 0.0015:
            _deduped_tps.append(tp)
    tps = _deduped_tps

    # Enforce monotonic RR — drop non-monotonic TPs
    _mono: list[float] = [tps[0]]
    prev_rr = _rr(entry_ref_mid, tps[0], sl, direction)
    for tp in tps[1:]:
        rr = _rr(entry_ref_mid, tp, sl, direction)
        if rr > prev_rr:
            _mono.append(tp)
            prev_rr = rr
    tps = _mono

    price_hint = float(p.get("price_hint") or entry_ref_mid or 0.0)
    ez_lo, ez_hi = _clamp_entry_zone_to_targets(
        ez_lo, ez_hi, tps, direction=direction, atr=atr, price_hint=price_hint
    )
    entry_ref_worst = _worst_zone_edge((ez_lo, ez_hi), direction)
    entry_ref_mid = _zone_midpoint((ez_lo, ez_hi))
    p["entry_zone"] = [ez_lo, ez_hi]

    # Zone clamping may have moved entry_ref_mid — drop TPs that no longer meet MIN_RR
    tps = [tp for tp in tps if _rr(entry_ref_mid, tp, sl, direction) >= _MIN_RR]
    if not tps:
        p["geometry_valid"] = False
        return p

    if direction == "long":
        tps = sorted(tps)
    else:
        tps = sorted(tps, reverse=True)

    # Pad tp2/tp3 slots from available structural TPs (repeat last if fewer than 3)
    while len(tps) < 3:
        tps.append(tps[-1])

    p["tp1"] = round(tps[0], 6)
    p["tp2"] = round(tps[1], 6)
    p["tp3"] = round(tps[2], 6)
    p["entry_reference"] = round(entry_ref_mid, 6)
    p["entry_reference_conservative"] = round(entry_ref_worst, 6)
    p["rr_tp1"] = round(_rr(entry_ref_mid, tps[0], sl, direction), 2)
    p["rr_tp2"] = round(_rr(entry_ref_mid, tps[1], sl, direction), 2)
    p["rr_tp3"] = round(_rr(entry_ref_mid, tps[2], sl, direction), 2)
    p["rr_conservative_tp1"] = round(_rr(entry_ref_worst, tps[0], sl, direction), 2)
    p["rr_conservative_tp2"] = round(_rr(entry_ref_worst, tps[1], sl, direction), 2)
    p["rr_conservative_tp3"] = round(_rr(entry_ref_worst, tps[2], sl, direction), 2)
    p["rr_base_label"] = "≈R:R (от середины зоны)"
    p["geometry_valid"] = plan_geometry_valid(p, direction=direction)
    return p


__all__ = ["finalize_plan_geometry", "plan_geometry_valid"]

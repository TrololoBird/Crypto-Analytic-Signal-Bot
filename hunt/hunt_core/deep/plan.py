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
    entry_ref = _worst_zone_edge((ez_lo, ez_hi), direction)
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
            if direction == "long" and f > entry_ref * 1.0001:
                tps.append(f)
            elif direction == "short" and f < entry_ref * 0.9999:
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
            tps.append(entry_ref + atr * mult)
        else:
            tps.append(entry_ref - atr * mult)

    for i, tp in enumerate(tps):
        rr = _rr(entry_ref, tp, sl, direction)
        if rr < _MIN_RR or rr > _MAX_RR:
            mult = (i + 2) * 1.5
            tps[i] = (entry_ref + atr * mult) if direction == "long" else (entry_ref - atr * mult)

    if direction == "long":
        tps = sorted(tps)
    else:
        tps = sorted(tps, reverse=True)

    prev_rr = 0.0
    for i, tp in enumerate(tps):
        rr = _rr(entry_ref, tp, sl, direction)
        if rr < prev_rr:
            mult = (i + 2) * 1.5
            tps[i] = (entry_ref + atr * mult) if direction == "long" else (entry_ref - atr * mult)
        prev_rr = max(prev_rr, _rr(entry_ref, tps[i], sl, direction))

    p["tp1"] = round(tps[0], 6)
    p["tp2"] = round(tps[1], 6)
    p["tp3"] = round(tps[2], 6)
    p["entry_reference"] = round(entry_ref, 6)
    p["rr_tp1"] = round(_rr(entry_ref, tps[0], sl, direction), 2)
    p["rr_tp2"] = round(_rr(entry_ref, tps[1], sl, direction), 2)
    p["rr_tp3"] = round(_rr(entry_ref, tps[2], sl, direction), 2)
    p["rr_base_label"] = "≈R:R (от края зоны)"
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


__all__ = ["build_trade_plan", "finalize_plan_geometry", "validate_plan_geometry"]

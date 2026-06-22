"""Deep trade plan — uses shared primitives."""
from __future__ import annotations

from typing import Any

from hunt_core.shared.primitives import atr_pad, forecast_band

_MAX_RR = 10.0
_MIN_RR = 0.3


def _rr(entry: float, target: float, stop: float, direction: str) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    reward = (target - entry) if direction == "long" else (entry - target)
    return reward / risk


def validate_plan_geometry(
    plan: dict[str, Any],
    *,
    direction: str,
    atr: float,
) -> dict[str, Any]:
    """Fix-in-place geometry errors: SL inside zone, scrambled TPs, 0R/absurd R.

    Returns a corrected copy (never raises).
    """
    p = dict(plan)
    ez = p.get("entry_zone") or [0.0, 0.0]
    ez_lo = float(ez[0]) if len(ez) >= 2 else 0.0
    ez_hi = float(ez[1]) if len(ez) >= 2 else 0.0
    ez_mid = (ez_lo + ez_hi) / 2.0
    sl = float(p.get("stop_loss") or p.get("stop") or 0.0)

    # 1. SL must be strictly outside the entry zone
    if direction == "long" and sl >= ez_lo and ez_lo > 0:
        sl = ez_lo - max(atr * 1.2, abs(ez_hi - ez_lo) * 0.5)
        p["stop_loss"] = round(sl, 6)
    elif direction == "short" and sl <= ez_hi and ez_hi > 0:
        sl = ez_hi + max(atr * 1.2, abs(ez_hi - ez_lo) * 0.5)
        p["stop_loss"] = round(sl, 6)

    entry = ez_mid if ez_mid > 0 else float(p.get("entry") or ez_mid)

    # 2. TPs must be on the correct side and monotonically ordered
    tps_raw = [p.get("tp1"), p.get("tp2"), p.get("tp3")]
    tps: list[float] = []
    for v in tps_raw:
        if v is None:
            continue
        try:
            f = float(v)
            if direction == "long" and f > entry * 1.0001:
                tps.append(f)
            elif direction == "short" and f < entry * 0.9999:
                tps.append(f)
        except (TypeError, ValueError):
            pass

    if direction == "long":
        tps = sorted(tps)
    else:
        tps = sorted(tps, reverse=True)

    # Fill missing TPs with ATR multiples
    while len(tps) < 3:
        mult = (len(tps) + 2) * 1.5
        if direction == "long":
            tps.append(entry + atr * mult)
        else:
            tps.append(entry - atr * mult)

    # 3. Reject 0R and clamp absurd R (>MAX_RR)
    for i, tp in enumerate(tps):
        rr = _rr(entry, tp, sl, direction)
        if rr < _MIN_RR or rr > _MAX_RR:
            mult = (i + 2) * 1.5
            tps[i] = (entry + atr * mult) if direction == "long" else (entry - atr * mult)

    p["tp1"] = round(tps[0], 6)
    p["tp2"] = round(tps[1], 6)
    if tps[2] is not None:
        p["tp3"] = round(tps[2], 6)
    return p


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
    return validate_plan_geometry(plan, direction=side, atr=atr)


__all__ = ["build_trade_plan", "validate_plan_geometry"]

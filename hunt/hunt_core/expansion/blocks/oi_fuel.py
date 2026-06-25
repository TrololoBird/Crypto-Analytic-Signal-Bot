"""Block 3 — OI fuel (price vs open-interest divergence).

OI building while price stays flat = positions accumulating = fuel for a move. Pure
magnitude with a mild directional lean from the price/OI divergence sign.
"""
from __future__ import annotations

from hunt_core.expansion._util import clamp01, opt_float, safe_float
from hunt_core.expansion.blocks._common import abstain, result
from hunt_core.expansion.types import BlockContext, BlockResult

NAME = "fuel"


def score(ctx: BlockContext) -> BlockResult:
    m = ctx.market
    oi_z = opt_float(m.get("oi_z"))
    map_oi_z = opt_float(m.get("map_oi_z"))
    oi_chg = opt_float(m.get("oi_chg_1h"))
    z = None
    for cand in (oi_z, map_oi_z):
        if cand is not None:
            z = cand if z is None else (z if abs(z) >= abs(cand) else cand)
    if z is None and oi_chg is None:
        return abstain(NAME)
    mag = clamp01(abs(z) / 3.0) if z is not None else clamp01(abs(oi_chg or 0) / 8.0)
    chg_24h = safe_float(ctx.row.get("chg_24h_pct"))
    evidence: list[str] = []
    direction = "neutral"
    rising = (oi_chg or 0) > 0 or (z or 0) > 0
    if rising and abs(chg_24h) < 3.0:
        direction = "up"
        evidence.append(f"oi_build_price_flat(z={z:.2f})" if z is not None else "oi_build_price_flat")
    elif rising and chg_24h <= -3.0:
        direction = "down"
        evidence.append("oi_build_into_weakness")
    return result(NAME, mag, direction=direction, evidence=tuple(evidence))


__all__ = ["NAME", "score"]

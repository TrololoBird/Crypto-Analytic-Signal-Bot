"""Block 20 — OI concentration (where the open interest sits vs price).

OI growth alone is neutral; OI *concentrated* just beyond price implies a liquidation
cascade once price reaches it. Approximated from OI z-score + the nearest cascade
direction/magnet on the row (full OI-bar distribution is a Phase-5 enrichment).
"""
from __future__ import annotations

from hunt_core.analysis.expansion_engine._util import clamp01, opt_float, pct_distance
from hunt_core.analysis.expansion_engine.blocks._common import abstain, result
from hunt_core.analysis.expansion_engine.types import BlockContext, BlockResult

NAME = "oi_concentration"


def score(ctx: BlockContext) -> BlockResult:
    m = ctx.market
    oi_z = opt_float(m.get("oi_z")) or opt_float(m.get("map_oi_z"))
    cascade = str(m.get("liq_cascade_risk") or "")
    if oi_z is None and not cascade:
        return abstain(NAME)

    mag = clamp01(abs(oi_z) / 3.0) if oi_z is not None else 0.4
    evidence: list[str] = []
    direction = "neutral"
    price = ctx.price
    if cascade == "short_squeeze":
        direction = "up"
        evidence.append("cascade_short_squeeze")
        magnet = opt_float(m.get("liq_heatmap_nearest_short"))
    elif cascade == "long_flush":
        direction = "down"
        evidence.append("cascade_long_flush")
        magnet = opt_float(m.get("liq_heatmap_nearest_long"))
    else:
        magnet = None

    if magnet is not None and price > 0:
        dist = pct_distance(price, magnet)
        mag = clamp01(mag * 0.6 + 0.4 * clamp01(1.0 - dist / 8.0))
        evidence.append(f"oi_magnet_{dist:.1f}%")
    if oi_z is not None and abs(oi_z) >= 1.5:
        evidence.append(f"oi_z={oi_z:.1f}")
    return result(NAME, mag, direction=direction, evidence=tuple(evidence))


__all__ = ["NAME", "score"]

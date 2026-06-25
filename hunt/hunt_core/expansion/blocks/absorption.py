"""Block 2 — Accumulation absorption (someone building a position).

Volume present while range stays tight: VP coil + sticky-bid absorption + bullish CVD.
Reuses the maps accumulation fusion rather than recomputing footprint.
"""
from __future__ import annotations

from hunt_core.expansion._util import opt_float
from hunt_core.expansion.blocks._common import abstain, result
from hunt_core.expansion.types import BlockContext, BlockResult

NAME = "absorption"


def score(ctx: BlockContext) -> BlockResult:
    m = ctx.market
    acc = opt_float(m.get("map_accumulation_score"))
    vp = opt_float(m.get("map_vp_accumulation"))
    bid_abs = bool(m.get("map_accum_bid_absorption"))
    cvd = str(m.get("map_cvd_divergence") or "")
    parts: list[float] = []
    evidence: list[str] = []
    if acc is not None:
        parts.append(acc)
        if acc >= 0.5:
            evidence.append(f"map_accum={acc:.2f}")
    if vp is not None:
        parts.append(vp)
    if bid_abs:
        parts.append(0.7)
        evidence.append("bid_absorption")
    if cvd == "bullish_div":
        parts.append(0.6)
        evidence.append("cvd_bullish_div")
    if not parts:
        return abstain(NAME)
    sval = sum(parts) / len(parts)
    direction = "up" if (bid_abs or cvd == "bullish_div" or (acc or 0) >= 0.4) else "neutral"
    return result(NAME, sval, direction=direction, evidence=tuple(evidence))


__all__ = ["NAME", "score"]

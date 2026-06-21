"""Block 9 — Supply exhaustion (sellers running out, before the rise).

Dedicated read of the "absorption-but-for-sellers" signature: bid absorption + net
sell flow that is *not* pushing price down + contracting volume/range. Distinct from
Block 2 (which reads accumulation broadly).
"""
from __future__ import annotations

from hunt_core.analysis.expansion_engine._util import clamp01, opt_float, safe_float
from hunt_core.analysis.expansion_engine.blocks._common import abstain, result
from hunt_core.analysis.expansion_engine.types import BlockContext, BlockResult

NAME = "supply_exhaustion"


def score(ctx: BlockContext) -> BlockResult:
    m = ctx.market
    bid_abs = bool(m.get("map_accum_bid_absorption"))
    delta = opt_float(m.get("agg_trade_delta"))
    vol = opt_float(m.get("vol_ratio")) or opt_float(ctx.tf("1h").get("vol_ratio"))
    chg_24h = safe_float(ctx.row.get("chg_24h_pct"))
    cvd = str(m.get("map_cvd_divergence") or "")

    have_signal = bid_abs or delta is not None or cvd == "bullish_div"
    if not have_signal:
        return abstain(NAME)

    parts: list[float] = []
    evidence: list[str] = []
    if bid_abs:
        parts.append(0.7)
        evidence.append("bid_absorption")
    # Net selling that fails to drop price = sellers absorbed.
    if delta is not None and delta < 0.45 and chg_24h > -1.5:
        parts.append(clamp01((0.5 - delta) * 2.0))
        evidence.append("sell_flow_absorbed")
    if cvd == "bullish_div":
        parts.append(0.6)
        evidence.append("cvd_bullish_div")
    # Falling volume during the stall reinforces exhaustion.
    if vol is not None and vol < 0.9:
        parts.append(clamp01((1.0 - vol)))
        evidence.append("volume_fade")
    if not parts:
        return abstain(NAME)
    sval = sum(parts) / len(parts)
    return result(NAME, sval, direction="up", evidence=tuple(evidence))


__all__ = ["NAME", "score"]

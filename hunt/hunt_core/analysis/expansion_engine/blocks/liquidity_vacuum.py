"""Block 17 — Liquidity vacuum (air pocket above/below).

Some of the strongest pumps start not from accumulation but from *thin* liquidity
overhead: a void/LVN gap with little resting volume means a market maker can push price
through it cheaply.
"""
from __future__ import annotations

from hunt_core.analysis.expansion_engine._util import clamp01, opt_float
from hunt_core.analysis.expansion_engine.blocks._common import abstain, result
from hunt_core.analysis.expansion_engine.types import BlockContext, BlockResult

NAME = "liquidity_vacuum"


def score(ctx: BlockContext) -> BlockResult:
    m = ctx.market
    void_above = opt_float(m.get("map_void_above"))
    void_pct = opt_float(m.get("map_void_above_pct"))
    ask_thinning = bool(m.get("map_ask_thinning"))
    if void_above is None and void_pct is None and not ask_thinning:
        return abstain(NAME)

    evidence: list[str] = []
    parts: list[float] = []
    if void_above is not None and ctx.price > 0 and void_above > ctx.price:
        # Closer void = cheaper push.
        dist = void_pct if void_pct is not None else abs(void_above - ctx.price) / ctx.price * 100.0
        parts.append(clamp01(1.0 - dist / 10.0))
        evidence.append(f"void_above_{dist:.1f}%")
    if ask_thinning:
        parts.append(0.6)
        evidence.append("ask_thinning")
    if not parts:
        return abstain(NAME)
    sval = sum(parts) / len(parts)
    return result(NAME, sval, direction="up", evidence=tuple(evidence))


__all__ = ["NAME", "score"]

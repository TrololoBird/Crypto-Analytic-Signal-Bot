"""Block 11 — Market-maker trap (false break → reclaim → impulse).

Bear trap (swept lows, reclaimed) leans pre-pump; bull trap (swept highs, rejected)
leans pre-dump. Reads the structure spine's sweep/reclaim signals — abstains when none
are present rather than guessing.
"""
from __future__ import annotations

from hunt_core.expansion._util import clamp01
from hunt_core.expansion.blocks._common import abstain, result
from hunt_core.expansion.types import BlockContext, BlockResult

NAME = "market_maker_trap"


def score(ctx: BlockContext) -> BlockResult:
    s = ctx.structure
    if not s:
        return abstain(NAME)
    event = str(s.get("event") or s.get("bos_choch") or "").lower()
    choch = bool(s.get("choch_detected")) or "choch" in event
    at_level = bool(s.get("at_level"))
    bos = str(s.get("bos_direction") or "")
    bsl_sweep = bool(s.get("bsl_sweep"))
    support_break = bool(s.get("support_break"))
    cvd = str(ctx.market.get("map_cvd_divergence") or "")

    # A trap needs a reclaim signature: sweep/CHoCH happening *at* a mapped level.
    has_trap = (choch and at_level) or bsl_sweep or support_break
    if not has_trap:
        return abstain(NAME)

    evidence: list[str] = []
    strength = 0.5
    if choch and at_level:
        strength += 0.2
        evidence.append("choch_at_level")
    direction = "neutral"
    if bos == "bull" or cvd == "bullish_div":
        direction = "up"
        evidence.append("bear_trap_reclaim")
        if cvd == "bullish_div":
            strength += 0.15
    elif bos == "bear" or cvd == "bearish_div":
        direction = "down"
        evidence.append("bull_trap_reject")
        if cvd == "bearish_div":
            strength += 0.15
    if bsl_sweep:
        evidence.append("bsl_sweep")
        strength += 0.1
    return result(NAME, clamp01(strength), direction=direction, evidence=tuple(evidence))


__all__ = ["NAME", "score"]

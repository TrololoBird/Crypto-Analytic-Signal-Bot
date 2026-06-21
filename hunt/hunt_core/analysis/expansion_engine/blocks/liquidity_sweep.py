"""Block 12 — Liquidity sweep (swept + reclaim, not merely "present").

There is a large difference between "liquidity above" and "liquidity *swept* above".
A sweep that reclaims back through the level is a strong pre-dump (after upside sweep)
or pre-pump (after downside sweep) tell.
"""
from __future__ import annotations

from hunt_core.analysis.expansion_engine._util import clamp01
from hunt_core.analysis.expansion_engine.blocks._common import abstain, result
from hunt_core.analysis.expansion_engine.features import structure_setup_type
from hunt_core.analysis.expansion_engine.types import BlockContext, BlockResult

NAME = "liquidity_sweep"


def score(ctx: BlockContext) -> BlockResult:
    s = ctx.structure
    if not s:
        return abstain(NAME)
    setup_type = str(s.get("setup_type") or "") or structure_setup_type(ctx)
    event = str(s.get("event") or s.get("bos_choch") or "").lower()
    at_level = bool(s.get("at_level"))
    bos = str(s.get("bos_direction") or "")

    is_sweep = "sweep" in setup_type or "sweep" in event or bool(s.get("bsl_sweep"))
    if not is_sweep:
        return abstain(NAME)

    evidence = ["sweep_reclaim" if "reclaim" in setup_type or at_level else "sweep"]
    strength = 0.55 + (0.15 if at_level else 0.0)
    direction = "neutral"
    if bos == "bull":
        direction = "up"
        evidence.append("swept_lows_reclaimed")
    elif bos == "bear":
        direction = "down"
        evidence.append("swept_highs_rejected")
    return result(NAME, clamp01(strength), direction=direction, evidence=tuple(evidence))


__all__ = ["NAME", "score"]

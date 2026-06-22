"""Block 6 — Market structure (accumulation vs distribution).

Reads the typed structure spine (HTF trend, BOS/CHoCH, bias) — HH/HL accumulation leans
pre-pump, LH/LL distribution leans pre-dump.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab.blocks._common import abstain, result
from hunt_core._dev.expansion_lab.types import BlockContext, BlockResult

NAME = "structure"


def score(ctx: BlockContext) -> BlockResult:
    s = ctx.structure
    if not s:
        return abstain(NAME)
    bias = str(s.get("structure_bias") or "wait")
    htf = str(s.get("htf_trend") or "neutral")
    bos = str(s.get("bos_direction") or "")
    choch = bool(s.get("choch_detected"))
    at_level = bool(s.get("at_level"))
    evidence: list[str] = []

    up = down = 0.0
    if bias == "long":
        up += 0.4
        evidence.append("bias_long")
    elif bias == "short":
        down += 0.4
        evidence.append("bias_short")
    if htf == "bull":
        up += 0.15
    elif htf == "bear":
        down += 0.15
    if bos == "bull":
        up += 0.15
        evidence.append("bos_bull")
    elif bos == "bear":
        down += 0.15
        evidence.append("bos_bear")
    if choch:
        evidence.append("choch")
        if bos == "bull":
            up += 0.1
        elif bos == "bear":
            down += 0.1
    if at_level:
        evidence.append("at_level")
        up += 0.05
        down += 0.05

    if up == 0.0 and down == 0.0:
        return abstain(NAME)
    if up >= down:
        return result(NAME, up, direction="up", evidence=tuple(evidence))
    return result(NAME, down, direction="down", evidence=tuple(evidence))


__all__ = ["NAME", "score"]

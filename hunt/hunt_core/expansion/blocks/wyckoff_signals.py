"""Block 22 — Wyckoff feature signals (not a full Wyckoff engine).

Four lightweight scores read from the structure spine:

    spring   → swept lows + reclaim        ⇒ pre-pump
    upthrust → swept highs + rejection      ⇒ pre-dump
    sos      → sign of strength (BOS up + vol)  ⇒ pre-pump confirmation
    sow      → sign of weakness (BOS down + vol) ⇒ pre-dump confirmation
"""
from __future__ import annotations

from hunt_core.expansion._util import clamp01, opt_float
from hunt_core.expansion.blocks._common import abstain, result
from hunt_core.expansion.features import structure_setup_type
from hunt_core.expansion.types import BlockContext, BlockResult


def _ctx_bits(ctx: BlockContext) -> dict:
    s = ctx.structure
    return {
        "event": str(s.get("event") or s.get("bos_choch") or "").lower(),
        "choch": bool(s.get("choch_detected")),
        "at_level": bool(s.get("at_level")),
        "bos": str(s.get("bos_direction") or ""),
        "setup": structure_setup_type(ctx),
        "vol": opt_float(ctx.market.get("vol_ratio")) or opt_float(ctx.tf("1h").get("vol_ratio")),
        "cvd": str(ctx.market.get("map_cvd_divergence") or ""),
    }


def score_spring(ctx: BlockContext) -> BlockResult:
    b = _ctx_bits(ctx)
    sweep = "sweep" in b["setup"] or "sweep" in b["event"]
    if not ((b["choch"] and b["at_level"] and b["bos"] == "bull") or (sweep and b["bos"] == "bull")):
        return abstain("wyckoff_spring")
    strength = 0.6 + (0.2 if b["cvd"] == "bullish_div" else 0.0)
    return result("wyckoff_spring", clamp01(strength), direction="up", evidence=("spring_reclaim",))


def score_upthrust(ctx: BlockContext) -> BlockResult:
    b = _ctx_bits(ctx)
    sweep = "sweep" in b["setup"] or "sweep" in b["event"]
    if not ((b["choch"] and b["at_level"] and b["bos"] == "bear") or (sweep and b["bos"] == "bear")):
        return abstain("wyckoff_upthrust")
    strength = 0.6 + (0.2 if b["cvd"] == "bearish_div" else 0.0)
    return result("wyckoff_upthrust", clamp01(strength), direction="down", evidence=("upthrust_reject",))


def score_sos(ctx: BlockContext) -> BlockResult:
    b = _ctx_bits(ctx)
    if b["bos"] != "bull":
        return abstain("wyckoff_sos")
    vol = b["vol"]
    strength = 0.5 + (clamp01((vol - 1.0)) if vol is not None and vol > 1.0 else 0.0)
    return result("wyckoff_sos", clamp01(strength), direction="up", evidence=("sign_of_strength",))


def score_sow(ctx: BlockContext) -> BlockResult:
    b = _ctx_bits(ctx)
    if b["bos"] != "bear":
        return abstain("wyckoff_sow")
    vol = b["vol"]
    strength = 0.5 + (clamp01((vol - 1.0)) if vol is not None and vol > 1.0 else 0.0)
    return result("wyckoff_sow", clamp01(strength), direction="down", evidence=("sign_of_weakness",))


__all__ = ["score_sos", "score_sow", "score_spring", "score_upthrust"]

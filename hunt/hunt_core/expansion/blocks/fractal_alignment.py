"""Block 14 — Strict multi-timeframe expansion alignment.

Not "do trends agree" — a *stage ladder*. The maximum rating is the canonical pre-pump
cascade:

    1D = accumulation · 4H = compression · 1H = fuel build · 15m = trigger

Conflicts (e.g. 1D distribution while 15m compresses) slash the rating. Each TF is
classified into a mini-stage from its own snapshot columns.
"""
from __future__ import annotations

from hunt_core.expansion._util import clamp01, opt_float
from hunt_core.expansion.blocks._common import abstain, result
from hunt_core.expansion.types import BlockContext, BlockResult

NAME = "fractal_alignment"

_TF_WEIGHTS = {"1d": 0.35, "4h": 0.30, "1h": 0.20, "15m": 0.15}


def _tf_stage(snap: dict) -> tuple[str, str]:
    """Return (stage, bias) for one TF snapshot. bias in up/down/neutral."""
    if not snap:
        return "none", "neutral"
    rsi = opt_float(snap.get("rsi14"))
    bb_pctile = opt_float(snap.get("bb_width_pctile"))
    squeeze = snap.get("squeeze_on") is True
    adx = opt_float(snap.get("adx14"))
    macd = opt_float(snap.get("macd_hist"))
    bias = "neutral"
    if macd is not None and macd > 0:
        bias = "up"
    elif macd is not None and macd < 0:
        bias = "down"

    if squeeze or (bb_pctile is not None and bb_pctile <= 0.2):
        return "compression", bias
    if rsi is not None and rsi >= 68:
        return "distribution", "down"
    if rsi is not None and rsi <= 32:
        return "accumulation", "up"
    if adx is not None and adx >= 25 and bias != "neutral":
        return ("trigger" if bias == "up" else "distribution"), bias
    if bias == "up":
        return "fuel", "up"
    if bias == "down":
        return "distribution", "down"
    return "neutral", "neutral"


def score(ctx: BlockContext) -> BlockResult:
    stages: dict[str, tuple[str, str]] = {}
    for tf_key in _TF_WEIGHTS:
        snap = ctx.tf(tf_key)
        if snap:
            stages[tf_key] = _tf_stage(snap)
    if not stages:
        return abstain(NAME)

    up_w = down_w = 0.0
    coil_bonus = 0.0
    evidence: list[str] = []
    for tf_key, (stage, bias) in stages.items():
        w = _TF_WEIGHTS[tf_key]
        if bias == "up" or stage in {"accumulation", "fuel"}:
            up_w += w
        elif bias == "down" or stage == "distribution":
            down_w += w
        if stage == "compression":
            coil_bonus += w * 0.5
        evidence.append(f"{tf_key}:{stage}")

    total = up_w + down_w
    if total <= 0 and coil_bonus <= 0:
        return abstain(NAME)
    direction = "up" if up_w >= down_w else "down"
    coherence = abs(up_w - down_w) / total if total else 0.0
    sval = clamp01(coherence + coil_bonus)

    # Hard penalty: top TF opposes the dominant lower-TF direction.
    top_stage, top_bias = stages.get("1d", ("none", "neutral"))
    if direction == "up" and top_bias == "down":
        sval *= 0.45
        evidence.append("htf_conflict")
    elif direction == "down" and top_bias == "up":
        sval *= 0.45
        evidence.append("htf_conflict")

    return result(NAME, sval, direction=direction, evidence=tuple(evidence[:5]))


__all__ = ["NAME", "score"]

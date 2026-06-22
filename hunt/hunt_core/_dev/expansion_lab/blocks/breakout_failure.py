"""Block 21 — Breakout failure (failed breakout → dump).

Strong dumps often start after an unconvincing breakout: price pokes above a prior high
on weak volume with no OI confirmation, then loses the level. Pre-dump evidence.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import clamp01, opt_float, safe_float
from hunt_core._dev.expansion_lab.blocks._common import abstain, result
from hunt_core._dev.expansion_lab.types import BlockContext, BlockResult

NAME = "breakout_failure"


def score(ctx: BlockContext) -> BlockResult:
    price = ctx.price
    if price <= 0:
        return abstain(NAME)
    snap = ctx.tf("1h") or ctx.tf("4h")
    prev_high = opt_float(snap.get("prev_high"))
    if prev_high is None or prev_high <= 0:
        return abstain(NAME)

    m = ctx.market
    vol = opt_float(m.get("vol_ratio")) or opt_float(snap.get("vol_ratio"))
    oi_chg = opt_float(m.get("oi_chg_1h"))
    rsi = opt_float(snap.get("rsi14"))

    # Did we recently poke above prev_high? Proximity within +/-1% counts as "tested".
    near_or_above = price >= prev_high * 0.992
    if not near_or_above:
        return abstain(NAME)

    parts: list[float] = []
    evidence: list[str] = []
    if vol is not None and vol < 1.0:
        parts.append(clamp01(1.0 - vol))
        evidence.append("weak_breakout_volume")
    if oi_chg is not None and oi_chg <= 0:
        parts.append(0.6)
        evidence.append("no_oi_confirmation")
    if rsi is not None and rsi >= 65:
        parts.append(clamp01((rsi - 65) / 20.0))
        evidence.append("overbought_at_break")
    # Already rejected back below the level.
    if price < prev_high:
        parts.append(0.5)
        evidence.append("back_below_high")
    if not parts:
        return abstain(NAME)
    sval = sum(parts) / len(parts)
    return result(NAME, sval, direction="down", evidence=tuple(evidence))


__all__ = ["NAME", "score"]

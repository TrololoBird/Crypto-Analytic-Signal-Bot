"""Block 13 — Distribution quality (crowd buys, smart money sells).

A rally is not automatically bullish. Price up + volume fading + net sell delta + OI
rising is textbook distribution — strong pre-dump evidence. Kept separate from the
bullish absorption block so the two never cancel.
"""
from __future__ import annotations

from hunt_core.expansion._util import clamp01, opt_float, safe_float
from hunt_core.expansion.blocks._common import abstain, result
from hunt_core.expansion.types import BlockContext, BlockResult

NAME = "distribution_quality"


def score(ctx: BlockContext) -> BlockResult:
    m = ctx.market
    chg_24h = safe_float(ctx.row.get("chg_24h_pct"))
    delta = opt_float(m.get("agg_trade_delta"))
    vol = opt_float(m.get("vol_ratio")) or opt_float(ctx.tf("1h").get("vol_ratio"))
    oi_chg = opt_float(m.get("oi_chg_1h"))
    cvd = str(m.get("map_cvd_divergence") or "")

    if chg_24h <= 0.5:
        return abstain(NAME)  # distribution only meaningful into strength

    parts: list[float] = []
    evidence: list[str] = []
    if delta is not None and delta < 0.5:
        parts.append(clamp01((0.5 - delta) * 2.0))
        evidence.append("sell_delta_into_rally")
    if vol is not None and vol < 1.0:
        parts.append(clamp01(1.0 - vol))
        evidence.append("volume_fade")
    if oi_chg is not None and oi_chg > 0:
        parts.append(clamp01(oi_chg / 10.0))
        evidence.append("oi_rising")
    if cvd == "bearish_div":
        parts.append(0.7)
        evidence.append("cvd_bearish_div")
    if not parts:
        return abstain(NAME)
    sval = sum(parts) / len(parts)
    # Stronger when the rally is extended.
    sval = clamp01(sval * (0.7 + 0.3 * clamp01(chg_24h / 10.0)))
    return result(NAME, sval, direction="down", evidence=tuple(evidence))


__all__ = ["NAME", "score"]

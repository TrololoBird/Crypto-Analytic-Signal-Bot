"""Block 23 — Volatility regime (multi-horizon ATR context).

Compression means different things after a high-volatility leg vs after a calm drift.
This block reads short- vs long-horizon ATR% and BB-width percentiles to score "coiled
after expansion" — the most explosive setup.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import clamp01, opt_float
from hunt_core._dev.expansion_lab.blocks._common import abstain, result
from hunt_core._dev.expansion_lab.types import BlockContext, BlockResult

NAME = "volatility_regime"


def score(ctx: BlockContext) -> BlockResult:
    short_atr = opt_float(ctx.tf("15m").get("atr_pct")) or opt_float(ctx.tf("1h").get("atr_pct"))
    long_atr = opt_float(ctx.tf("4h").get("atr_pct")) or opt_float(ctx.tf("1d").get("atr_pct"))
    bb_pctile = opt_float(ctx.tf("1h").get("bb_width_pctile"))
    if short_atr is None and long_atr is None and bb_pctile is None:
        return abstain(NAME)

    evidence: list[str] = []
    parts: list[float] = []
    # Coiled-after-expansion: current (short) vol contracted relative to higher TF range.
    if short_atr is not None and long_atr is not None and long_atr > 0:
        ratio = short_atr / long_atr
        # ratio < 1 means near-term calmer than the larger swing — energy stored.
        parts.append(clamp01(1.0 - ratio))
        if ratio < 0.7:
            evidence.append(f"vol_contraction({ratio:.2f})")
    if bb_pctile is not None:
        parts.append(1.0 - bb_pctile)
        if bb_pctile <= 0.2:
            evidence.append("bb_low_regime")
    if not parts:
        return abstain(NAME)
    sval = sum(parts) / len(parts)
    return result(NAME, sval, direction="up", evidence=tuple(evidence))


__all__ = ["NAME", "score"]

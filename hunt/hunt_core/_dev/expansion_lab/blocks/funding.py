"""Block 4 — Funding state (asymmetric, contrarian).

Crowded longs (high positive funding) lean pre-dump; negative funding with price not
falling is classic pre-pump / short-squeeze fuel.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import clamp01, opt_float, safe_float
from hunt_core._dev.expansion_lab.blocks._common import abstain, result
from hunt_core._dev.expansion_lab.types import BlockContext, BlockResult

NAME = "funding"


def score(ctx: BlockContext) -> BlockResult:
    m = ctx.market
    fz = opt_float(m.get("funding_zscore_48h"))
    fpct = opt_float(m.get("funding_pct"))
    rate = opt_float(m.get("funding_rate"))
    if fz is None and fpct is None and rate is None:
        return abstain(NAME)
    # Magnitude from z-score when available, else absolute funding %.
    if fz is not None:
        mag = clamp01(abs(fz) / 3.0)
        signed = fz
    else:
        ref = fpct if fpct is not None else (rate * 100.0 if rate is not None else 0.0)
        mag = clamp01(abs(ref) / 0.05)
        signed = ref
    chg_24h = safe_float(ctx.row.get("chg_24h_pct"))
    evidence: list[str] = []
    direction = "neutral"
    if signed > 0:
        direction = "down"
        evidence.append(f"crowded_longs(f={signed:+.3f})")
    elif signed < 0:
        # Negative funding + price holding => pre-pump fuel.
        if chg_24h > -2.0:
            direction = "up"
            evidence.append(f"neg_funding_holding(f={signed:+.3f})")
        else:
            mag *= 0.5
    return result(NAME, mag, direction=direction, evidence=tuple(evidence))


__all__ = ["NAME", "score"]

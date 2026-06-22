"""Block 1 — Volatility compression (energy accumulating).

Low BB-width percentile + low ATR + an active squeeze means coiled energy. Reads the
already-computed per-TF coil columns; abstains on a thin row.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import opt_float, smooth_down
from hunt_core._dev.expansion_lab.blocks._common import abstain, result
from hunt_core._dev.expansion_lab.types import BlockContext, BlockResult

NAME = "compression"


def score(ctx: BlockContext) -> BlockResult:
    coil_parts: list[float] = []
    evidence: list[str] = []
    squeeze_hits = 0
    for tf_key in ("4h", "1h", "15m"):
        snap = ctx.tf(tf_key)
        if not snap:
            continue
        pctile = opt_float(snap.get("bb_width_pctile"))
        if pctile is not None:
            coil_parts.append(1.0 - pctile)  # low width pctile => high coil
        if snap.get("squeeze_on") is True:
            squeeze_hits += 1
        dwp = opt_float(snap.get("donchian_width_pct"))
        if dwp is not None:
            coil_parts.append(smooth_down(dwp, lo=1.0, hi=8.0))
    if not coil_parts and squeeze_hits == 0:
        return abstain(NAME)
    base = sum(coil_parts) / len(coil_parts) if coil_parts else 0.5
    sval = base + 0.12 * squeeze_hits
    if squeeze_hits:
        evidence.append(f"squeeze_on×{squeeze_hits}")
    if base >= 0.7:
        evidence.append(f"coil={base:.2f}")
    return result(NAME, sval, direction="up", evidence=tuple(evidence))


__all__ = ["NAME", "score"]

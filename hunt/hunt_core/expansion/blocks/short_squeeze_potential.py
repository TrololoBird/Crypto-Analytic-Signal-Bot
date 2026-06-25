"""Block 18 — Short-squeeze potential (pre-pump fuel).

Crowded shorts + negative funding + price not falling + magnets above = a short squeeze
waiting to fire. Reuses the maps squeeze-fuel model (``liq_squeeze_fuel_short``).
"""
from __future__ import annotations

from hunt_core.expansion._util import opt_float
from hunt_core.expansion.blocks._common import abstain, result
from hunt_core.expansion.types import BlockContext, BlockResult

NAME = "short_squeeze_potential"


def score(ctx: BlockContext) -> BlockResult:
    fuel = opt_float(ctx.market.get("liq_squeeze_fuel_short"))
    if fuel is None:
        return abstain(NAME)
    evidence = (f"short_squeeze_fuel={fuel:.2f}",) if fuel >= 0.4 else ()
    return result(NAME, fuel, direction="up", evidence=evidence)


__all__ = ["NAME", "score"]

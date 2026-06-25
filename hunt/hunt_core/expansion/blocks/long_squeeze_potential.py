"""Block 19 — Long-squeeze potential (pre-dump fuel).

Crowded longs + positive funding + magnets below = a long flush waiting to fire. Mirror
of Block 18 via ``liq_squeeze_fuel_long``.
"""
from __future__ import annotations

from hunt_core.expansion._util import opt_float
from hunt_core.expansion.blocks._common import abstain, result
from hunt_core.expansion.types import BlockContext, BlockResult

NAME = "long_squeeze_potential"


def score(ctx: BlockContext) -> BlockResult:
    fuel = opt_float(ctx.market.get("liq_squeeze_fuel_long"))
    if fuel is None:
        return abstain(NAME)
    evidence = (f"long_squeeze_fuel={fuel:.2f}",) if fuel >= 0.4 else ()
    return result(NAME, fuel, direction="down", evidence=evidence)


__all__ = ["NAME", "score"]

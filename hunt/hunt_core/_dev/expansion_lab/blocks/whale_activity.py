"""Block 24 — Whale activity (coefficient, not a standalone signal).

Resting iceberg / sticky-wall presence and the nearer wall side add conviction behind
whichever side the directional blocks already favour. Never fires a signal alone.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import clamp01, opt_float, pct_distance
from hunt_core._dev.expansion_lab.blocks._common import abstain, result
from hunt_core._dev.expansion_lab.types import BlockContext, BlockResult

NAME = "whale_activity"


def score(ctx: BlockContext) -> BlockResult:
    m = ctx.market
    iceberg = m.get("map_iceberg_count")
    sticky = m.get("map_sticky_wall_count")
    bid_wall = opt_float(m.get("nearest_bid_wall"))
    ask_wall = opt_float(m.get("nearest_ask_wall"))
    if iceberg is None and sticky is None and bid_wall is None and ask_wall is None:
        return abstain(NAME)

    parts: list[float] = []
    evidence: list[str] = []
    try:
        ice_n = int(iceberg or 0)
    except (TypeError, ValueError):
        ice_n = 0
    try:
        sticky_n = int(sticky or 0)
    except (TypeError, ValueError):
        sticky_n = 0
    if ice_n:
        parts.append(clamp01(ice_n / 4.0))
        evidence.append(f"iceberg×{ice_n}")
    if sticky_n:
        parts.append(clamp01(sticky_n / 4.0))
        evidence.append(f"sticky_walls×{sticky_n}")

    direction = "neutral"
    price = ctx.price
    if price > 0 and bid_wall and ask_wall:
        bid_dist = pct_distance(price, bid_wall)
        ask_dist = pct_distance(price, ask_wall)
        if bid_dist < ask_dist:
            direction = "up"
            evidence.append("bid_wall_support")
        elif ask_dist < bid_dist:
            direction = "down"
            evidence.append("ask_wall_resistance")
    elif bid_wall and not ask_wall:
        direction = "up"
    elif ask_wall and not bid_wall:
        direction = "down"

    if not parts:
        parts.append(0.4)
    sval = sum(parts) / len(parts)
    return result(NAME, sval, direction=direction, evidence=tuple(evidence))


__all__ = ["NAME", "score"]

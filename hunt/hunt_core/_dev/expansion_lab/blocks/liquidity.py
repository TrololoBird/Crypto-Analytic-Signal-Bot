"""Block 5 — Liquidity asymmetry (where it is profitable to drive price).

More / closer liquidity above than below ⇒ upside draw (pre-pump); the mirror leans
pre-dump. Reuses the maps target collectors so we count the same magnets the forecast
layer uses.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import clamp01, pct_distance
from hunt_core._dev.expansion_lab.blocks._common import abstain, result
from hunt_core._dev.expansion_lab.types import BlockContext, BlockResult

NAME = "liquidity"


def _nearest_pct(targets: list[float], price: float, *, above: bool) -> float | None:
    side = [t for t in targets if (t > price if above else t < price)]
    if not side:
        return None
    nearest = min(side, key=lambda t: abs(t - price))
    return pct_distance(price, nearest)


def score(ctx: BlockContext) -> BlockResult:
    price = ctx.price
    if price <= 0:
        return abstain(NAME)
    try:
        from hunt_core.shared.primitives.targets import (
            collect_downward_targets as _collect_downward_targets,
            collect_upward_targets as _collect_upward_targets,
        )

        up_targets, up_f = _collect_upward_targets(ctx.row, price)
        down_targets, down_f = _collect_downward_targets(ctx.row, price)
    except Exception:
        up_targets, up_f, down_targets, down_f = [], [], [], []

    pools = ctx.structure.get("liquidity_pools") if isinstance(ctx.structure.get("liquidity_pools"), dict) else {}
    eqh = len(pools.get("equal_highs") or [])
    eql = len(pools.get("equal_lows") or [])

    up_count = len(up_targets) + eqh
    down_count = len(down_targets) + eql
    if up_count == 0 and down_count == 0:
        return abstain(NAME)

    total = up_count + down_count
    upside = up_count / total if total else 0.5
    evidence: list[str] = []
    direction = "neutral"
    if upside >= 0.6:
        direction = "up"
        evidence.append(f"upside_liq×{up_count}")
    elif upside <= 0.4:
        direction = "down"
        evidence.append(f"downside_liq×{down_count}")
    asymmetry = abs(upside - 0.5) * 2.0
    # Closer liquidity is stronger fuel.
    up_pct = _nearest_pct(up_targets, price, above=True)
    proximity = clamp01(1.0 - (up_pct or 12.0) / 12.0) if direction == "up" else 0.4
    sval = clamp01(0.4 * asymmetry + 0.4 * proximity + 0.2 * clamp01(total / 6.0))
    for f in (up_f + down_f)[:2]:
        if f not in evidence:
            evidence.append(f)
    return result(NAME, sval, direction=direction, evidence=tuple(evidence))


__all__ = ["NAME", "score"]

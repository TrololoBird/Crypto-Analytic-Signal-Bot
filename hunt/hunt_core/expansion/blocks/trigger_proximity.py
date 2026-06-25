"""Block 10 — Trigger proximity (how close is the launch, not how ready).

A coil can sit for days. This block estimates nearness to ignition from the distance to
the nearest activation level (void / equal high / nearest magnet) combined with coil and
delta momentum. Its output *is* ``trigger_probability``.
"""
from __future__ import annotations

from hunt_core.expansion._util import clamp01, opt_float, pct_distance, safe_float
from hunt_core.expansion.blocks._common import abstain, result
from hunt_core.expansion.types import BlockContext, BlockDeltas, BlockResult

NAME = "trigger_proximity"


def activation_distance_pct(ctx: BlockContext, *, direction: str) -> float | None:
    """Percent distance to the nearest activation level on the dominant side."""
    price = ctx.price
    if price <= 0:
        return None
    m = ctx.market
    candidates: list[float] = []
    if direction == "up":
        void = opt_float(m.get("map_void_above"))
        if void is not None and void > price:
            candidates.append(void)
        short_liq = opt_float(m.get("liq_heatmap_nearest_short"))
        if short_liq is not None and short_liq > price:
            candidates.append(short_liq)
        pools = ctx.structure.get("liquidity_pools") if isinstance(ctx.structure.get("liquidity_pools"), dict) else {}
        na = opt_float(pools.get("nearest_above"))
        if na is not None and na > price:
            candidates.append(na)
    else:
        long_liq = opt_float(m.get("liq_heatmap_nearest_long"))
        if long_liq is not None and long_liq < price:
            candidates.append(long_liq)
        pools = ctx.structure.get("liquidity_pools") if isinstance(ctx.structure.get("liquidity_pools"), dict) else {}
        nb = opt_float(pools.get("nearest_below"))
        if nb is not None and nb < price:
            candidates.append(nb)
    if not candidates:
        return None
    nearest = min(candidates, key=lambda c: abs(c - price))
    return pct_distance(price, nearest)


def score(
    ctx: BlockContext,
    *,
    blocks: dict[str, BlockResult],
    deltas: BlockDeltas,
    direction: str,
) -> BlockResult:
    comp = blocks.get("compression")
    fuel = blocks.get("fuel_imbalance")
    comp_v = comp.score if comp and comp.active else 0.0
    fuel_v = fuel.score if fuel and fuel.active else 0.0
    dist = activation_distance_pct(ctx, direction=direction if direction != "neutral" else "up")
    evidence: list[str] = []

    if dist is None and comp_v == 0.0 and fuel_v == 0.0:
        return abstain(NAME)

    # Closer activation => higher proximity. 0% → 1.0, 15%+ → 0.0.
    proximity = clamp01(1.0 - (dist if dist is not None else 12.0) / 15.0)
    if dist is not None:
        evidence.append(f"activation_{dist:.1f}%")
    momentum = safe_float(getattr(deltas, "momentum", 0.5), 0.5)
    sval = clamp01(0.45 * proximity + 0.20 * momentum + 0.20 * comp_v + 0.15 * fuel_v)
    if momentum > 0.6:
        evidence.append("delta_accelerating")
    return result(NAME, sval, direction=direction, evidence=tuple(evidence))


__all__ = ["NAME", "activation_distance_pct", "score"]

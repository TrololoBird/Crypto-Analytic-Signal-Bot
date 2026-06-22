"""Level 3 — Execution Engine.

Builds the actionable geometry for a qualifying expansion: entry zone, activation level
(the break that confirms ignition), protective stop, and laddered targets. This is the
expansion scenario's own geometry — it is *not* the Verdict trade plan.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import opt_float, safe_float
from hunt_core._dev.expansion_lab.types import BlockContext, ExpansionExecution


def _atr(ctx: BlockContext) -> float:
    for tf_key in ("1h", "4h", "15m"):
        snap = ctx.tf(tf_key)
        atr = opt_float(snap.get("atr14"))
        if atr and atr > 0:
            return atr
        atr_pct = opt_float(snap.get("atr_pct"))
        if atr_pct and ctx.price > 0:
            return ctx.price * atr_pct / 100.0
    return ctx.price * 0.01 if ctx.price > 0 else 0.0


def _activation_level(ctx: BlockContext, direction: str) -> float | None:
    price = ctx.price
    m = ctx.market
    pools = ctx.structure.get("liquidity_pools") if isinstance(ctx.structure.get("liquidity_pools"), dict) else {}
    cands: list[float] = []
    if direction == "up":
        for v in (m.get("map_void_above"), m.get("liq_heatmap_nearest_short"), pools.get("nearest_above")):
            fv = opt_float(v)
            if fv is not None and fv > price:
                cands.append(fv)
    else:
        for v in (m.get("liq_heatmap_nearest_long"), pools.get("nearest_below")):
            fv = opt_float(v)
            if fv is not None and fv < price:
                cands.append(fv)
    if not cands:
        return None
    return min(cands, key=lambda c: abs(c - price))


def _targets(ctx: BlockContext, direction: str, *, limit: int = 3) -> list[float]:
    price = ctx.price
    try:
        from hunt_core.shared.primitives.targets import (
            collect_downward_targets as _collect_downward_targets,
            collect_upward_targets as _collect_upward_targets,
        )

        if direction == "up":
            raw, _ = _collect_upward_targets(ctx.row, price)
            side = sorted({round(t, 8) for t in raw if t > price})
        else:
            raw, _ = _collect_downward_targets(ctx.row, price)
            side = sorted({round(t, 8) for t in raw if t < price}, reverse=True)
    except Exception:
        side = []
    return side[:limit]


def build_execution(ctx: BlockContext, *, direction: str) -> ExpansionExecution | None:
    price = ctx.price
    if direction not in {"up", "down"} or price <= 0:
        return None
    atr = _atr(ctx)
    if atr <= 0:
        return None

    pools = ctx.structure.get("liquidity_pools") if isinstance(ctx.structure.get("liquidity_pools"), dict) else {}
    if direction == "up":
        entry_band = (round(price - 0.6 * atr, 8), round(price + 0.2 * atr, 8))
        support = opt_float(pools.get("nearest_below"))
        stop = round(min(support, price - 1.5 * atr) if support else price - 1.5 * atr, 8)
    else:
        entry_band = (round(price - 0.2 * atr, 8), round(price + 0.6 * atr, 8))
        resistance = opt_float(pools.get("nearest_above"))
        stop = round(max(resistance, price + 1.5 * atr) if resistance else price + 1.5 * atr, 8)

    activation = _activation_level(ctx, direction)
    if activation is None:
        activation = round(price + (0.8 * atr if direction == "up" else -0.8 * atr), 8)

    targets = _targets(ctx, direction)
    if not targets:
        # ATR-laddered fallback.
        step = atr * (1.0 if direction == "up" else -1.0)
        targets = [round(price + step * k, 8) for k in (1.5, 3.0, 5.0)]
    return ExpansionExecution(
        entry_band=entry_band,
        activation=round(float(activation), 8),
        stop=stop,
        targets=tuple(targets),
    )


__all__ = ["build_execution"]

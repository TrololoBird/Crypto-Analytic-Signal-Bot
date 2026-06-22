"""Level 2 — Forecast Engine.

Turns a qualifying expansion state into a scenario: expected move band, expected
horizon, and the main drivers behind it. Move magnitude is anchored on liquidity-map
distance (where price is likely drawn) widened by setup quality; horizon is a coil-age
heuristic (calibrated later from the outcome ledger).
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import clamp01, pct_distance
from hunt_core._dev.expansion_lab.types import (
    BlockContext,
    BlockResult,
    ExpansionForecast,
)

_FALLBACK_MOVE = {"up": (12.0, 35.0), "down": (-12.0, -35.0)}


def _liquidity_targets(ctx: BlockContext, direction: str) -> list[float]:
    price = ctx.price
    if price <= 0:
        return []
    try:
        from hunt_core.shared.primitives.targets import (
            collect_downward_targets as _collect_downward_targets,
            collect_upward_targets as _collect_upward_targets,
        )

        if direction == "up":
            targets, _ = _collect_upward_targets(ctx.row, price)
            return [t for t in targets if t > price]
        targets, _ = _collect_downward_targets(ctx.row, price)
        return [t for t in targets if t < price]
    except Exception:
        return []


def build_forecast(
    ctx: BlockContext,
    *,
    direction: str,
    blocks: dict[str, BlockResult],
    expansion_quality: float,
    trigger_probability: float,
) -> ExpansionForecast | None:
    if direction not in {"up", "down"} or ctx.price <= 0:
        return None
    price = ctx.price
    targets = _liquidity_targets(ctx, direction)

    # Move band anchored on the farthest mapped magnet, widened by quality.
    if targets:
        dists = sorted({round(pct_distance(price, t), 2) for t in targets})
        lo = dists[0] if dists else 8.0
        hi = max(dists) if dists else 25.0
    else:
        lo, hi = (abs(v) for v in _FALLBACK_MOVE[direction])
    # Quality stretches the upper bound (stronger coils travel farther).
    hi = hi * (1.0 + 0.6 * expansion_quality)
    lo = max(lo, hi * 0.25)
    if direction == "down":
        move = (-round(hi, 1), -round(lo, 1))
    else:
        move = (round(lo, 1), round(hi, 1))

    # Horizon: closer trigger ⇒ sooner. Persistence widens patience.
    persistence = blocks.get("state_persistence")
    pscore = persistence.score if persistence and persistence.active else 0.0
    near = clamp01(trigger_probability)
    h_lo = 12.0 + (1.0 - near) * 60.0
    h_hi = 72.0 + (1.0 - near) * 120.0 + pscore * 96.0
    horizon = (round(h_lo, 1), round(h_hi, 1))

    drivers = _main_drivers(blocks, direction)
    return ExpansionForecast(
        expected_move_pct=move,
        expected_horizon_h=horizon,
        main_drivers=drivers,
    )


def _main_drivers(blocks: dict[str, BlockResult], direction: str, *, top_n: int = 4) -> tuple[str, ...]:
    ranked = sorted(
        (r for r in blocks.values() if r.active and r.evidence and r.direction in {direction, "neutral"}),
        key=lambda r: r.score,
        reverse=True,
    )
    drivers: list[str] = []
    for r in ranked:
        if r.evidence:
            drivers.append(r.evidence[0])
        if len(drivers) >= top_n:
            break
    return tuple(drivers)


__all__ = ["build_forecast"]

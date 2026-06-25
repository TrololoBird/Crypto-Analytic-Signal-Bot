"""Block registry — runs every scorer over a :class:`BlockContext`.

Base blocks (Tiers A/B/C minus the two history/delta-dependent ones) are pure functions
of the row. ``state_persistence`` (needs the history buffer) and ``trigger_proximity``
(needs deltas + a preliminary direction) are computed by the orchestrator after the base
pass, so they are intentionally absent here.
"""
from __future__ import annotations

from typing import Callable

from hunt_core.expansion.blocks import (
    absorption,
    breakout_failure,
    compression,
    cycle_context,
    distribution_quality,
    fractal_alignment,
    fuel_imbalance,
    funding,
    liquidity,
    liquidity_sweep,
    liquidity_vacuum,
    long_squeeze_potential,
    market_maker_trap,
    oi_concentration,
    oi_fuel,
    relative_strength,
    short_squeeze_potential,
    structure as structure_block,
    supply_exhaustion,
    volatility_regime,
    whale_activity,
    wyckoff_signals,
)
from hunt_core.expansion.types import BlockContext, BlockResult

# Single-result base scorers.
_BASE_SCORERS: tuple[Callable[[BlockContext], BlockResult], ...] = (
    compression.score,
    absorption.score,
    oi_fuel.score,
    funding.score,
    liquidity.score,
    structure_block.score,
    relative_strength.score,
    fuel_imbalance.score,
    supply_exhaustion.score,
    market_maker_trap.score,
    liquidity_sweep.score,
    distribution_quality.score,
    fractal_alignment.score,
    cycle_context.score,
    liquidity_vacuum.score,
    short_squeeze_potential.score,
    long_squeeze_potential.score,
    oi_concentration.score,
    breakout_failure.score,
    volatility_regime.score,
    whale_activity.score,
)

# Wyckoff emits four named sub-scores.
_WYCKOFF_SCORERS: tuple[Callable[[BlockContext], BlockResult], ...] = (
    wyckoff_signals.score_spring,
    wyckoff_signals.score_upthrust,
    wyckoff_signals.score_sos,
    wyckoff_signals.score_sow,
)


def score_base_blocks(ctx: BlockContext) -> dict[str, BlockResult]:
    """Run all base block scorers; one failing scorer never sinks the tick."""
    out: dict[str, BlockResult] = {}
    for fn in _BASE_SCORERS + _WYCKOFF_SCORERS:
        try:
            res = fn(ctx)
        except Exception:  # pragma: no cover - defensive, abstain on bad input
            continue
        out[res.name] = res
    return out


def _block_names() -> tuple[str, ...]:
    from hunt_core.expansion.types import BlockScores

    return tuple(BlockScores.__dataclass_fields__.keys())


__all__: list[str] = ["score_base_blocks"]

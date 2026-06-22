"""OpportunityScore — the single criterion for universe TOP-N ranking.

Most scanners rank by pre-pump score alone, which over-ranks coins that are "ready" but
far from a trigger or sitting in thin liquidity. OpportunityScore multiplies the factors
that actually matter for catching the move early.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import clamp01


def compute_opportunity_score(
    *,
    expansion_quality: float,
    trigger_probability: float,
    liquidity_score: float,
    cycle_score: float,
    fake_breakout_risk: float,
    rotation_score: float | None = None,
) -> float:
    # Factors in [0.5, 1.0] so a single weak (but present) input dampens rather than
    # zeroes the rank. Missing rotation is treated as neutral.
    def f(x: float) -> float:
        return 0.5 + 0.5 * clamp01(x)

    rot = 0.7 if rotation_score is None else f(rotation_score)
    core = clamp01(expansion_quality) * clamp01(trigger_probability)
    score = core * f(liquidity_score) * f(cycle_score) * rot
    score *= 1.0 - 0.5 * clamp01(fake_breakout_risk)
    return round(clamp01(score), 4)


__all__ = ["compute_opportunity_score"]

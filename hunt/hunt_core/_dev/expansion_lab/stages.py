"""Lifecycle stage classifier (1–6) and state derivation.

The 6-stage pump anatomy:

    1 capitulation → 2 accumulation → 3 compression → 4 fuel build → 5 trigger → 6 expansion

The engine targets stages 3→4. State (pre_pump / pre_dump / accumulation / ...) is
derived *after* the probability model, never before.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab.config import ExpansionConfig
from hunt_core._dev.expansion_lab.types import (
    BlockResult,
    ExpansionProbabilities,
    ExpansionStateKind,
)

_STAGE_LABELS = {
    1: "Capitulation",
    2: "Accumulation",
    3: "Compression",
    4: "Fuel Build",
    5: "Trigger",
    6: "Expansion",
}


def classify_lifecycle_stage(
    blocks: dict[str, BlockResult],
    trigger_probability: float,
) -> tuple[int, str]:
    def s(name: str) -> float:
        r = blocks.get(name)
        return r.score if r and r.active else 0.0

    compression = s("compression")
    absorption = max(s("absorption"), s("supply_exhaustion"))
    fuel = max(s("fuel_imbalance"), s("fuel"))
    distribution = s("distribution_quality")

    if trigger_probability >= 0.7:
        stage = 5
    elif fuel >= 0.55 and compression >= 0.45:
        stage = 4
    elif compression >= 0.6:
        stage = 3
    elif absorption >= 0.5 or distribution >= 0.5:
        stage = 2
    elif compression >= 0.35 or fuel >= 0.35:
        stage = 3
    else:
        stage = 1
    return stage, _STAGE_LABELS[stage]


def derive_state(
    probs: ExpansionProbabilities,
    *,
    trigger_probability: float,
    blocks: dict[str, BlockResult],
    cfg: ExpansionConfig,
) -> ExpansionStateKind:
    up, down, none = probs.p_up, probs.p_down, probs.p_none

    def s(name: str) -> float:
        r = blocks.get(name)
        return r.score if r and r.active else 0.0

    # Active expansion: trigger already very close and one side clearly dominant.
    if trigger_probability >= 0.82:
        if up >= down and up > none:
            return "active_pump"
        if down > up and down > none:
            return "active_dump"

    strong = cfg.state_strong_prob
    ratio = cfg.state_dominance_ratio
    pivot = cfg.state_pivot_prob

    if up >= strong and up >= down * ratio:
        return "pre_pump"
    if down >= strong and down >= up * ratio:
        return "pre_dump"

    # Ambiguous pivot zone — both sides elevated, direction undecided.
    if up >= pivot and down >= pivot:
        return "distribution" if s("distribution_quality") >= s("absorption") else "accumulation"

    if up >= pivot and up > down:
        return "accumulation"
    if down >= pivot and down > up:
        return "distribution"
    return "neutral"


__all__ = ["classify_lifecycle_stage", "derive_state"]

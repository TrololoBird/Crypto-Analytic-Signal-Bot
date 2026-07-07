"""Score computation from state machine progress.

Score 0..1 per pattern type. Higher = more complete pattern, more confidence.
"""
from __future__ import annotations

from hunt_core.scanner.detect.state import SymbolState


def compute_score_a(state: SymbolState) -> float:
    score = 0.0
    checks = 0

    if state.impulse_detected:
        score += 0.20
    checks += 1

    if state.absorption_detected:
        score += 0.25
        if state.one_candle_absorb:
            score += 0.05
    checks += 1

    n = len(state.bokoviks)
    if n >= 1:
        bonus = min(0.10, state.bokoviks[0].touches * 0.02)
        score += 0.20 + bonus
    if n >= 2:
        score += 0.10
        checks += 0.5
    checks += 1

    if state.sweep is not None:
        score += 0.20
    checks += 1

    if state.structure_broken:
        score += 0.15
    checks += 1

    if checks == 0:
        return 0.0
    return min(1.0, score)


def compute_score_b(state: SymbolState) -> float:
    score = 0.0
    checks = 0

    if state.macro_extreme > 0:
        score += 0.15
    checks += 1

    if state.sweep is not None:
        score += 0.25
    checks += 1

    if state.candle_fade or state.instant_rejection:
        score += 0.20
    checks += 1

    if state.structure_broken:
        score += 0.20
    checks += 1

    if state.ltf_confirmed:
        score += 0.20
    checks += 1

    if checks == 0:
        return 0.0
    return min(1.0, score)


SCORE_HIGH = 0.70
SCORE_MEDIUM = 0.50
SCORE_LOW = 0.30

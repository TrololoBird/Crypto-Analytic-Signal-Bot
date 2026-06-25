"""Meta scores — expansion_quality, fake_breakout_risk, readiness.

``expansion_quality`` is the setup-quality measure (independent of direction);
``fake_breakout_risk`` penalizes it and suppresses execution; ``readiness`` is the
human-facing composite of score + trigger + quality.
"""
from __future__ import annotations

from hunt_core.expansion._util import clamp01
from hunt_core.expansion.config import ExpansionConfig
from hunt_core.expansion.types import (
    BlockResult,
    ExpansionProbabilities,
    Readiness,
)


def _score(blocks: dict[str, BlockResult], name: str) -> float:
    r = blocks.get(name)
    return r.score if r and r.active else 0.0


def fake_breakout_risk(blocks: dict[str, BlockResult]) -> float:
    """High when a "breakout" lacks the confirmations that make moves stick.

    Drivers: low volume, no OI confirmation, no upside liquidity, weak structure, no
    persistence. Each missing confirmation adds risk.
    """
    risk = 0.0
    evidence_weight = 0.0
    fuel = _score(blocks, "fuel_imbalance")
    if fuel < 0.3:
        risk += 0.25
        evidence_weight += 1
    if _score(blocks, "liquidity") < 0.3 and _score(blocks, "liquidity_vacuum") < 0.3:
        risk += 0.2
        evidence_weight += 1
    if _score(blocks, "structure") < 0.3:
        risk += 0.2
        evidence_weight += 1
    if _score(blocks, "state_persistence") < 0.2:
        risk += 0.2
        evidence_weight += 1
    if _score(blocks, "absorption") < 0.25 and _score(blocks, "supply_exhaustion") < 0.25:
        risk += 0.15
        evidence_weight += 1
    return clamp01(risk)


def expansion_quality(
    blocks: dict[str, BlockResult],
    *,
    fake_risk: float,
    cfg: ExpansionConfig,
) -> float:
    weights = cfg.quality_weights
    total = 0.0
    wsum = 0.0
    for name, w in weights.items():
        total += w * _score(blocks, name)
        wsum += w
    base = total / wsum if wsum else 0.0
    fractal = _score(blocks, "fractal_alignment")
    multiplier = 0.8 + 0.4 * fractal  # alignment scales the whole setup
    return clamp01(base * multiplier * (1.0 - 0.6 * fake_risk))


def readiness(
    *,
    expansion_score: float,
    trigger_probability: float,
    quality: float,
    fake_risk: float,
) -> Readiness:
    composite = (expansion_score + trigger_probability + quality) / 3.0
    if composite >= 0.62 and fake_risk < 0.4:
        return "high"
    if composite >= 0.45:
        return "medium"
    return "low"


def risk_label(*, fake_risk: float, probabilities: ExpansionProbabilities, coverage: float) -> str:
    ambiguity = 1.0 - abs(probabilities.p_up - probabilities.p_down)
    score = 0.5 * fake_risk + 0.3 * ambiguity + 0.2 * (1.0 - coverage)
    if score >= 0.6:
        return "high"
    if score >= 0.4:
        return "medium"
    return "low"


__all__ = ["expansion_quality", "fake_breakout_risk", "readiness", "risk_label"]

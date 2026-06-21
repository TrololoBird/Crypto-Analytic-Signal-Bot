"""Scenario fragility."""
from __future__ import annotations

from hunt_core.analysis.deep.verdict_v2._helpers import clamp01
from hunt_core.analysis.deep.verdict_v2.config import VerdictV2Config
from hunt_core.analysis.deep.verdict_v2.types import (
    DisagreementState,
    ExpectedPath,
    HorizonTopology,
    PatternConfidence,
    ScenarioFragility,
)


def compute_fragility(
    path: ExpectedPath,
    topo: HorizonTopology,
    disagree: DisagreementState,
    patterns: PatternConfidence,
    cfg: VerdictV2Config,
) -> ScenarioFragility:
    score = 0.25
    deps: list[str] = []
    breaks: list[str] = []
    evidence: list[str] = []

    if patterns.ambiguous:
        score += 0.15
        deps.append("pattern_clarity")
        breaks.append("alternative pattern wins")
    if disagree.score > 0.35:
        score += disagree.score * 0.35
        deps.append("engine_consensus")
        breaks.append(f"conflict on {disagree.dominant_conflict or 'engines'}")
    if topo.kind in {"mixed", "reversal_candidate"}:
        score += 0.12
        deps.append("horizon_alignment")
        breaks.append(f"topology shifts from {topo.kind}")
    if path.direction == "neutral":
        score += 0.2
        deps.append("directional_commit")
        breaks.append("range expansion without bias")

    score = clamp01(score)
    if score >= cfg.fragility_high_threshold:
        label = "high"
    elif score >= 0.4:
        label = "moderate"
    else:
        label = "low"
    evidence.append(f"topo={topo.kind}")
    evidence.append(f"disagree={disagree.state}")
    return ScenarioFragility(
        score=round(score, 3),
        label=label,  # type: ignore[arg-type]
        dependencies=deps,
        break_conditions=breaks,
        evidence=evidence,
    )

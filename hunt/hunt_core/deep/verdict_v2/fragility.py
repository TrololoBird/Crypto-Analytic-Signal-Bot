"""Scenario fragility."""
from __future__ import annotations

from typing import Any

from hunt_core.deep.verdict_v2._helpers import clamp01
from hunt_core.deep.verdict_v2.config import VerdictV2Config
from hunt_core.deep.verdict_v2.types import (
    DisagreementState,
    ExpectedPath,
    HorizonTopology,
    PatternConfidence,
    ScenarioFragility,
    TradePlan,
)


def compute_fragility(
    path: ExpectedPath,
    topo: HorizonTopology,
    disagree: DisagreementState,
    patterns: PatternConfidence,
    cfg: VerdictV2Config,
    *,
    plan: TradePlan | None = None,
    row: dict[str, Any] | None = None,
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

    if plan is not None and row is not None and plan.direction in {"long", "short"}:
        from hunt_core.deep.verdict_v2.levels import stop_structural_buffer_atr

        entry_ref = plan.entry_reference or (plan.entry_zone[0] + plan.entry_zone[1]) / 2
        buf_atr = stop_structural_buffer_atr(row, plan.direction, plan.stop_loss, entry_ref)
        if buf_atr <= 0.05:
            score += 0.22
            deps.append("stop_buffer")
            breaks.append("стоп без буфера за структурой")
            evidence.append("stop_tight_vs_structure")
        elif buf_atr < 0.2:
            score += 0.10
            evidence.append("stop_thin_buffer")

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

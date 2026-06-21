"""Level 1 orchestrator — build_expansion_opportunity(row).

Single entry point. Runs blocks → deltas/persistence → trigger proximity →
probability model → quality/meta → stage/state → forecast (L2) → execution (L3).
Pure read of the tick ``row``; never imports verdict_v2.
"""
from __future__ import annotations

from typing import Any

from hunt_core.analysis.expansion_engine.blocks import score_base_blocks
from hunt_core.analysis.expansion_engine.blocks import state_persistence as persistence_block
from hunt_core.analysis.expansion_engine.blocks import trigger_proximity as trigger_block
from hunt_core.analysis.expansion_engine.config import ExpansionConfig, load_expansion_config
from hunt_core.analysis.expansion_engine.deltas import compute_deltas
from hunt_core.analysis.expansion_engine.execution import build_execution
from hunt_core.analysis.expansion_engine.forecast import build_forecast
from hunt_core.analysis.expansion_engine.history import ExpansionHistory, global_history
from hunt_core.analysis.expansion_engine.probability_model import ExpansionProbabilityModel
from hunt_core.analysis.expansion_engine.quality import (
    expansion_quality,
    fake_breakout_risk,
    readiness,
    risk_label,
)
from hunt_core.analysis.expansion_engine.ranking.opportunity_score import compute_opportunity_score
from hunt_core.analysis.expansion_engine.stages import classify_lifecycle_stage, derive_state
from hunt_core.analysis.expansion_engine.state_machine import (
    ExpansionStateMachine,
    global_state_machine,
)
from hunt_core.analysis.expansion_engine.types import (
    BlockContext,
    BlockResult,
    ExpansionOpportunity,
    MetaScores,
)


def _prelim_direction(blocks: dict[str, BlockResult]) -> str:
    up = sum(r.score for r in blocks.values() if r.active and r.direction == "up")
    down = sum(r.score for r in blocks.values() if r.active and r.direction == "down")
    if up == 0.0 and down == 0.0:
        return "neutral"
    return "up" if up >= down else "down"


def _block_score(blocks: dict[str, BlockResult], name: str) -> float:
    r = blocks.get(name)
    return r.score if r and r.active else 0.0


def _collect_drivers(blocks: dict[str, BlockResult], direction: str, *, top_n: int = 4) -> tuple[str, ...]:
    ranked = sorted(
        (r for r in blocks.values() if r.active and r.evidence and r.direction in {direction, "neutral"}),
        key=lambda r: r.score,
        reverse=True,
    )
    drivers: list[str] = []
    seen: set[str] = set()
    for r in ranked:
        ev = r.evidence[0]
        if ev in seen:
            continue
        seen.add(ev)
        drivers.append(ev)
        if len(drivers) >= top_n:
            break
    return tuple(drivers)


def build_expansion_opportunity(
    row: dict[str, Any],
    *,
    history: ExpansionHistory | None = None,
    state_machine: ExpansionStateMachine | None = None,
    cfg: ExpansionConfig | None = None,
) -> ExpansionOpportunity:
    cfg = cfg or load_expansion_config()
    history = history if history is not None else global_history()
    fsm = state_machine if state_machine is not None else global_state_machine()

    ctx = BlockContext.from_row(row)
    blocks = score_base_blocks(ctx)

    # Record base scores (incl. current) so persistence dwell counts this tick.
    base_scores = {name: (r.score if r.active else 0.0) for name, r in blocks.items()}
    history.record(ctx.symbol, base_scores)

    scores_obj = _scores_from(blocks)
    deltas = compute_deltas(ctx.symbol, scores_obj, history, cfg)

    persistence = persistence_block.score(ctx, history=history, cfg=cfg)
    blocks[persistence.name] = persistence

    prelim = _prelim_direction(blocks)
    trigger = trigger_block.score(ctx, blocks=blocks, deltas=deltas, direction=prelim)
    blocks[trigger.name] = trigger
    trigger_probability = trigger.score if trigger.active else 0.0

    stage_int, stage_label = classify_lifecycle_stage(blocks, trigger_probability)
    model = ExpansionProbabilityModel(cfg)
    probs = model.predict(blocks, deltas, stage_int)
    expansion_score = round(max(probs.p_up, probs.p_down), 4)

    fake_risk = round(fake_breakout_risk(blocks), 4)
    quality = round(expansion_quality(blocks, fake_risk=fake_risk, cfg=cfg), 4)

    derived = derive_state(probs, trigger_probability=trigger_probability, blocks=blocks, cfg=cfg)
    state = fsm.transition(ctx.symbol, derived)

    coverage = round(_coverage(blocks), 4)
    rdy = readiness(
        expansion_score=expansion_score,
        trigger_probability=trigger_probability,
        quality=quality,
        fake_risk=fake_risk,
    )
    risk = risk_label(fake_risk=fake_risk, probabilities=probs, coverage=coverage)

    liquidity_score = max(_block_score(blocks, "liquidity"), _block_score(blocks, "liquidity_vacuum"))
    cycle_score = _block_score(blocks, "cycle_context")
    opportunity_score = compute_opportunity_score(
        expansion_quality=quality,
        trigger_probability=trigger_probability,
        liquidity_score=liquidity_score,
        cycle_score=cycle_score,
        fake_breakout_risk=fake_risk,
        rotation_score=None,
    )
    meta = MetaScores(
        expansion_quality=quality,
        fake_breakout_risk=fake_risk,
        opportunity_score=opportunity_score,
        sector_rotation=None,
    )

    dominant = "up" if probs.p_up >= probs.p_down and probs.p_up > probs.p_none else (
        "down" if probs.p_down > probs.p_up and probs.p_down > probs.p_none else "neutral"
    )
    drivers = _collect_drivers(blocks, dominant if dominant != "neutral" else "up")

    forecast = None
    execution = None
    if dominant != "neutral" and quality >= cfg.forecast_min_quality:
        forecast = build_forecast(
            ctx,
            direction=dominant,
            blocks=blocks,
            expansion_quality=quality,
            trigger_probability=trigger_probability,
        )
        if (
            trigger_probability >= cfg.execution_min_trigger
            and fake_risk < cfg.fake_breakout_block
        ):
            execution = build_execution(ctx, direction=dominant)

    evidence = tuple(
        e
        for r in sorted(blocks.values(), key=lambda r: r.score, reverse=True)
        if r.active
        for e in r.evidence
    )[:8]

    return ExpansionOpportunity(
        symbol=ctx.symbol,
        price=ctx.price,
        state=state,
        stage=stage_label,
        lifecycle_stage=stage_int,
        probabilities=probs,
        expansion_score=expansion_score,
        trigger_probability=round(trigger_probability, 4),
        meta=meta,
        blocks=scores_obj_with(blocks),
        deltas=deltas,
        main_drivers=drivers,
        readiness=rdy,
        risk=risk,
        coverage=coverage,
        forecast=forecast,
        execution=execution,
        evidence=evidence,
    )


def _scores_from(blocks: dict[str, BlockResult]):
    from hunt_core.analysis.expansion_engine.types import BlockScores

    valid = {
        name: (r.score if r.active else 0.0)
        for name, r in blocks.items()
        if name in BlockScores.__dataclass_fields__
    }
    return BlockScores(**valid)


def scores_obj_with(blocks: dict[str, BlockResult]):
    return _scores_from(blocks)


def _coverage(blocks: dict[str, BlockResult]) -> float:
    if not blocks:
        return 0.0
    active = sum(1 for r in blocks.values() if r.active)
    return active / len(blocks)


def opportunity_from_row(
    row: dict[str, Any],
    *,
    cfg: ExpansionConfig | None = None,
    history: ExpansionHistory | None = None,
    state_machine: ExpansionStateMachine | None = None,
    prefer_stamped: bool = True,
) -> ExpansionOpportunity:
    """Use stamped ``row["expansion"]`` when present; else full orchestrator pass."""
    if prefer_stamped:
        exp = row.get("expansion")
        if isinstance(exp, dict) and exp.get("probabilities") and exp.get("meta"):
            try:
                return ExpansionOpportunity.from_dict(exp)
            except (TypeError, ValueError, KeyError):
                pass
    return build_expansion_opportunity(row, cfg=cfg, history=history, state_machine=state_machine)


__all__ = ["build_expansion_opportunity", "opportunity_from_row"]

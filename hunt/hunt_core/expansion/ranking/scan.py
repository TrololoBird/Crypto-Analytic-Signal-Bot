"""Universe scan — TOP-N PRE-PUMP and PRE-DUMP lists by OpportunityScore.

Scanning is where rotation context becomes available: only across the full universe can
we tell whether a symbol's expansion energy is rising *relative to everything else*. The
scan therefore recomputes OpportunityScore with the cross-universe rotation score folded
in (a single-symbol probe leaves rotation neutral).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from hunt_core.expansion._util import clamp01
from hunt_core.expansion.config import ExpansionConfig, load_expansion_config
from hunt_core.expansion.ranking.opportunity_score import compute_opportunity_score
from hunt_core.expansion.rotation.engine import compute_rotation_scores
from hunt_core.expansion.types import ExpansionOpportunity


def _opportunity_from(item: Any, cfg: ExpansionConfig) -> ExpansionOpportunity | None:
    if isinstance(item, ExpansionOpportunity):
        return item
    if isinstance(item, dict):
        from hunt_core.expansion.expansion.orchestrator import (
            opportunity_from_row,
        )

        try:
            return opportunity_from_row(item, cfg=cfg, prefer_stamped=True)
        except Exception:
            return None
    return None


def _apply_rotation(
    opp: ExpansionOpportunity,
    rotation: float | None,
) -> ExpansionOpportunity:
    """Recompute OpportunityScore with the rotation score folded in (frozen-safe)."""
    if rotation is None:
        return opp
    liquidity_score = max(opp.blocks.liquidity, opp.blocks.liquidity_vacuum)
    cycle_score = opp.blocks.cycle_context
    new_opp_score = compute_opportunity_score(
        expansion_quality=opp.meta.expansion_quality,
        trigger_probability=opp.trigger_probability,
        liquidity_score=liquidity_score,
        cycle_score=cycle_score,
        fake_breakout_risk=opp.meta.fake_breakout_risk,
        rotation_score=rotation,
    )
    new_meta = replace(
        opp.meta,
        opportunity_score=new_opp_score,
        sector_rotation=round(clamp01(rotation), 4),
    )
    return replace(opp, meta=new_meta)


def rank_universe(
    items: Iterable[Any],
    *,
    cfg: ExpansionConfig | None = None,
    top_n: int | None = None,
    tiers: dict[str, str] | None = None,
) -> dict[str, list[ExpansionOpportunity]]:
    """Rank rows/opportunities into TOP-N pre-pump and pre-dump lists.

    ``items`` may be tick rows (dicts) or pre-built :class:`ExpansionOpportunity`. The
    cross-universe rotation score is computed here and merged into each OpportunityScore.
    """
    cfg = cfg or load_expansion_config()
    limit = top_n if top_n is not None else cfg.scan_top_n

    opportunities: list[ExpansionOpportunity] = []
    for item in items:
        opp = _opportunity_from(item, cfg)
        if opp is not None:
            opportunities.append(opp)

    rotation_scores = compute_rotation_scores(opportunities, tiers=tiers)
    opportunities = [_apply_rotation(o, rotation_scores.get(o.symbol)) for o in opportunities]

    pump = [o for o in opportunities if o.dominant == "up"]
    dump = [o for o in opportunities if o.dominant == "down"]
    pump.sort(key=lambda o: o.meta.opportunity_score, reverse=True)
    dump.sort(key=lambda o: o.meta.opportunity_score, reverse=True)
    return {"pre_pump": pump[:limit], "pre_dump": dump[:limit]}


__all__ = ["rank_universe"]

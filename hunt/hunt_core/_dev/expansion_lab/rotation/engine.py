"""Rotation Engine — where capital is flowing across the universe.

Standalone subpackage (never imports verdict_v2). Capital rotates BTC → ETH → large →
mid → low → memes; a symbol whose expansion energy is rising relative to the rest of the
universe is receiving rotation. v1 proxy: percentile rank of opportunity-energy within
the scanned universe, optionally bucketed by a caller-supplied tier map.
"""
from __future__ import annotations

from typing import Iterable

from hunt_core._dev.expansion_lab._util import clamp01
from hunt_core._dev.expansion_lab.types import ExpansionOpportunity


def _energy(opp: ExpansionOpportunity) -> float:
    return max(opp.meta.opportunity_score, opp.expansion_score * opp.meta.expansion_quality)


def compute_rotation_scores(
    opportunities: Iterable[ExpansionOpportunity],
    *,
    tiers: dict[str, str] | None = None,
) -> dict[str, float]:
    """Map symbol → rotation score (0..1) = within-universe energy percentile.

    When ``tiers`` is supplied (symbol → tier label), ranking is done within each tier so
    a hot low-cap is not buried under large-cap energy.
    """
    opps = list(opportunities)
    if not opps:
        return {}

    groups: dict[str, list[ExpansionOpportunity]] = {}
    for o in opps:
        tier = (tiers or {}).get(o.symbol, "_all")
        groups.setdefault(tier, []).append(o)

    scores: dict[str, float] = {}
    for group in groups.values():
        ranked = sorted(group, key=_energy)
        n = len(ranked)
        for i, o in enumerate(ranked):
            pct = (i + 1) / n if n > 1 else 1.0
            scores[o.symbol] = round(clamp01(pct), 4)
    return scores


def sector_rotation_score(symbol: str, rotation_scores: dict[str, float]) -> float | None:
    return rotation_scores.get(symbol.upper())


__all__ = ["compute_rotation_scores", "sector_rotation_score"]

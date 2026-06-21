"""Universe Ranking Engine — OpportunityScore + TOP-N scan."""
from __future__ import annotations

from hunt_core.analysis.expansion_engine.ranking.opportunity_score import compute_opportunity_score
from hunt_core.analysis.expansion_engine.ranking.scan import rank_universe

__all__ = ["compute_opportunity_score", "rank_universe"]

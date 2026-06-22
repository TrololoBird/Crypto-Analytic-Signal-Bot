"""Universe Ranking Engine — OpportunityScore + TOP-N scan."""
from __future__ import annotations

from hunt_core._dev.expansion_lab.ranking.opportunity_score import compute_opportunity_score
from hunt_core._dev.expansion_lab.ranking.scan import rank_universe

__all__ = ["compute_opportunity_score", "rank_universe"]

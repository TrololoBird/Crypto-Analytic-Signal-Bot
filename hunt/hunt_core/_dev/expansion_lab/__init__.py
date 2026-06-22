"""Expansion Engine — standalone PRE-PUMP / PRE-DUMP discovery stack.

A separate module from Verdict V2. Both read the same assembled tick ``row`` but never
import each other and never merge scores. The Expansion Engine answers "which coins are
in a state from which pumps/dumps are born, and why might one start *now*?" — returning
an :class:`ExpansionOpportunity`, not a long/short verdict.

Three levels live under this package:
  Level 1  ``expansion/``  — state + probabilities (``build_expansion_opportunity``)
  Level 2  ``forecast/``   — expected move / horizon / drivers
  Level 3  ``execution/``  — entry / activation / SL / TP1–TP3

Plus ``ranking/`` (OpportunityScore, TOP-N scan), ``learning/`` (outcome ledger +
calibration), and ``rotation/`` (cross-universe capital flow).
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab.config import ExpansionConfig, load_expansion_config
from hunt_core._dev.expansion_lab.expansion import (
    build_expansion_opportunity,
    opportunity_from_row,
)
from hunt_core._dev.expansion_lab.format import (
    format_expansion_card,
    format_expansion_section,
    format_scan,
)
from hunt_core._dev.expansion_lab.ranking import compute_opportunity_score, rank_universe
from hunt_core._dev.expansion_lab.types import (
    ExpansionExecution,
    ExpansionForecast,
    ExpansionOpportunity,
    ExpansionProbabilities,
)


def build_expansion_dict(row: dict) -> dict:
    """Convenience: build and serialize the opportunity for stamping on a row."""
    return build_expansion_opportunity(row).to_dict()


__all__ = [
    "ExpansionConfig",
    "ExpansionExecution",
    "ExpansionForecast",
    "ExpansionOpportunity",
    "ExpansionProbabilities",
    "build_expansion_dict",
    "build_expansion_opportunity",
    "compute_opportunity_score",
    "format_expansion_card",
    "format_expansion_section",
    "format_scan",
    "load_expansion_config",
    "opportunity_from_row",
    "rank_universe",
]

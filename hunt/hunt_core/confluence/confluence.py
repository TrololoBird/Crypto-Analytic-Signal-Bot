"""MTF family-voting confluence + must-pass gate (P6)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hunt_core.confluence.mtf import MTFConfluence, ScenarioScore, TFSignal, build_mtf_confluence


@dataclass(frozen=True, slots=True)
class ConfluenceVote:
    family: str
    direction: str
    weight: float


FAMILIES: tuple[str, ...] = ("trend", "momentum", "flow", "derivatives", "structure")


def family_vote_count(confluence: MTFConfluence | dict[str, Any]) -> int:
    """One vote per family — anti double-count (§3)."""
    if isinstance(confluence, MTFConfluence):
        data = confluence.to_dict() if hasattr(confluence, "to_dict") else confluence.__dict__
    else:
        data = confluence
    votes = data.get("family_votes") or data.get("votes") or {}
    if isinstance(votes, dict):
        return sum(1 for v in votes.values() if v)
    return int(data.get("aligned_count") or data.get("score") or 0)


def evaluate_must_pass(row: dict[str, Any], *, direction: str) -> tuple[bool, list[str]]:
    """Must-pass triggers separate from strength rank (§E.3)."""
    setup = row.get("dump") if direction == "short" else row.get("long")
    if not isinstance(setup, dict):
        return False, ["no_setup"]
    missing: list[str] = []
    if not setup.get("confirmed") and float(setup.get("dump_score" if direction == "short" else "long_score") or 0) < 45:
        missing.append("min_fuel")
    lc = row.get("lifecycle") or {}
    bias = str(lc.get("recommended_bias") or "wait")
    if direction == "short" and bias == "long":
        missing.append("htf_bias_veto")
    if direction == "long" and bias == "short":
        missing.append("htf_bias_veto")
    return len(missing) == 0, missing


__all__ = [
    "ConfluenceVote",
    "FAMILIES",
    "MTFConfluence",
    "ScenarioScore",
    "TFSignal",
    "build_mtf_confluence",
    "evaluate_must_pass",
    "family_vote_count",
]

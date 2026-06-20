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
FAMILY_VOTE_MIN = 2  # min HTF-aligned families for confirm delivery (§E.3)


def family_vote_count(
    confluence: MTFConfluence | dict[str, Any],
    *,
    direction: str = "",
) -> int:
    """One vote per family — anti double-count (§3)."""
    if isinstance(confluence, MTFConfluence):
        if direction == "short":
            return int(confluence.short_scenario.htf_count)
        if direction == "long":
            return int(confluence.long_scenario.htf_count)
        data = confluence.to_dict() if hasattr(confluence, "to_dict") else confluence.__dict__
    elif isinstance(confluence, dict):
        data = confluence
        if direction == "short":
            hc = data.get("short_htf_count")
            if hc is not None:
                return int(hc)
            sc = data.get("short_scenario")
            if isinstance(sc, dict) and sc.get("htf_count") is not None:
                return int(sc["htf_count"])
        if direction == "long":
            hc = data.get("long_htf_count")
            if hc is not None:
                return int(hc)
            sc = data.get("long_scenario")
            if isinstance(sc, dict) and sc.get("htf_count") is not None:
                return int(sc["htf_count"])
    else:
        return 0
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
    if not setup.get("confirmed") and float(setup.get("delivery_ev") or setup.get("ev_primary_ev") or 0) <= 0:
        p_win = float(setup.get("delivery_p_win") or setup.get("p_win") or 0)
        if p_win < 0.35:
            missing.append("min_ev")
    lc = row.get("lifecycle") or {}
    bias = str(lc.get("recommended_bias") or "wait")
    phase = str(lc.get("phase") or "")
    fall_pct = float(lc.get("fall_from_high_pct") or 0)
    if direction == "short" and bias == "long":
        distribution_fade = phase in {"exhaustion_at_high", "distribution"} and bool(
            setup.get("confirmed")
        )
        if not distribution_fade:
            missing.append("htf_bias_veto")
    if direction == "long" and bias == "short":
        long_leg = phase in {
            "post_dump_bounce",
            "accumulation",
            "recovery",
            "breakout_arming",
            "impulse_initiating",
        }
        if not (long_leg and bool(setup.get("confirmed"))):
            missing.append("htf_bias_veto")
    from hunt_core.gate.policy import mtf_confirm_veto

    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    mkt = row.get("market") if isinstance(row.get("market"), dict) else {}
    bounce_pct = float(lc.get("bounce_from_low_pct") or 0)
    mtf_blocked, mtf_reason = mtf_confirm_veto(
        direction,
        tf,
        phase,
        market=mkt,
        fall_from_high_pct=fall_pct,
        bounce_from_low_pct=bounce_pct,
    )
    if mtf_blocked and mtf_reason:
        missing.append(mtf_reason)
    return len(missing) == 0, missing


__all__ = [
    "ConfluenceVote",
    "FAMILIES",
    "FAMILY_VOTE_MIN",
    "MTFConfluence",
    "ScenarioScore",
    "TFSignal",
    "build_mtf_confluence",
    "evaluate_must_pass",
    "family_vote_count",
]

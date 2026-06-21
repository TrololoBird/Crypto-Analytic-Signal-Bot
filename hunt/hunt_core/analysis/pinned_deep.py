"""Module 2 pinned verdict engine — structure-first, independent of hunt fusion."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from hunt_core.analysis.trend_engine import trend_from_snapshot
from hunt_core.confluence.mtf import ScenarioScore
from hunt_core.data.universe import PINNED_SYMBOLS

PanelDir = Literal["long", "short", "neutral"]
VerdictKind = Literal["long", "short", "sideways"]

_PANEL_TFS = ("1w", "1d", "4h", "1h", "15m", "5m")
_PANEL_WEIGHTS = {"1w": 3.0, "1d": 2.5, "4h": 2.0, "1h": 1.5, "15m": 1.0, "5m": 0.75}


@dataclass(frozen=True, slots=True)
class IndicatorPanel:
    dominant: PanelDir
    long_votes: int
    short_votes: int
    total_votes: int
    long_score: float
    short_score: float
    votes_by_tf: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PinnedVerdict:
    kind: VerdictKind
    confidence: float
    long_scenario: ScenarioScore
    short_scenario: ScenarioScore
    reason: str


def is_pinned_symbol(symbol: str) -> bool:
    return str(symbol or "").upper() in PINNED_SYMBOLS


def build_pinned_indicator_panel(
    symbol: str,
    tf: dict[str, Any],
) -> IndicatorPanel:
    """Vote across TF snapshots — EMA stack + DI from prepare extended pack."""
    long_w = 0.0
    short_w = 0.0
    long_votes = 0
    short_votes = 0
    votes_by_tf: dict[str, str] = {}

    for key in _PANEL_TFS:
        snap = tf.get(key) or {}
        if not snap or snap.get("status") == "empty":
            continue
        weight = _PANEL_WEIGHTS.get(key, 1.0)
        trend = trend_from_snapshot(snap, require_adx=False)
        adx = float(snap.get("adx14") or 0)
        if adx > 0 and adx < 20:
            votes_by_tf[key] = "neutral"
            continue
        if trend == "bull":
            long_w += weight
            long_votes += 1
            votes_by_tf[key] = "long"
        elif trend == "bear":
            short_w += weight
            short_votes += 1
            votes_by_tf[key] = "short"
        else:
            votes_by_tf[key] = "neutral"

    total = long_votes + short_votes
    denom = long_w + short_w
    long_score = round(long_w / denom, 3) if denom > 0 else 0.0
    short_score = round(short_w / denom, 3) if denom > 0 else 0.0

    if long_score >= short_score + 0.12:
        dominant: PanelDir = "long"
    elif short_score >= long_score + 0.12:
        dominant = "short"
    else:
        dominant = "neutral"

    return IndicatorPanel(
        dominant=dominant,
        long_votes=long_votes,
        short_votes=short_votes,
        total_votes=total,
        long_score=long_score,
        short_score=short_score,
        votes_by_tf=votes_by_tf,
    )



def _scenario_from_verdict_v2(v2: Any, direction: str) -> ScenarioScore:
    plan = v2.trade_plan
    h_b = v2.horizons.get("B")
    score = float(v2.signal_strength.score)
    if direction == "long" and h_b:
        score = float(h_b.long)
    elif direction == "short" and h_b:
        score = float(h_b.short)
    entry_lo = entry_hi = tp1 = tp2 = stop = 0.0
    if plan and plan.direction == direction:
        entry_lo, entry_hi = plan.entry_zone
        tp1, tp2 = plan.take_profit_1, plan.take_profit_2
        stop = plan.stop_loss
    topo = v2.horizon_topology
    from hunt_core.analysis.deep.verdict_v2._helpers import direction_bias

    htf_count = sum(
        1
        for dom in (topo.a_dominant, topo.b_dominant)
        if direction_bias(str(dom)) == direction
    )
    htf_total = sum(
        1 for dom in (topo.a_dominant, topo.b_dominant) if direction_bias(str(dom)) in {"long", "short"}
    )
    return ScenarioScore(
        direction=direction,  # type: ignore[arg-type]
        score=round(score, 3),
        htf_count=htf_count,
        htf_total=max(htf_total, 1),
        entry_lo=entry_lo,
        entry_hi=entry_hi,
        tp1=tp1,
        tp2=tp2,
        stop=stop,
        evidence=list(v2.evidence[:4]),
    )


def build_pinned_verdict(row: dict[str, Any]) -> PinnedVerdict:
    """Verdict V2 scenario engine — compat shim to PinnedVerdict."""
    from hunt_core.analysis.deep.verdict_v2.orchestrator import build_scenario_verdict

    v2 = build_scenario_verdict(row)
    action = v2.signal_decision.action
    if action == "long":
        kind: VerdictKind = "long"
        confidence = v2.signal_strength.score
    elif action == "short":
        kind = "short"
        confidence = v2.signal_strength.score
    else:
        kind = "sideways"
        confidence = max(0.35, 1.0 - v2.signal_strength.score)

    long_s = _scenario_from_verdict_v2(v2, "long")
    short_s = _scenario_from_verdict_v2(v2, "short")
    reason = v2.signal_decision.reason
    row["verdict_v2"] = v2
    from hunt_core.analysis.deep.verdict_v2.serialize import attach_verdict_v2_to_row

    attach_verdict_v2_to_row(row)
    return PinnedVerdict(
        kind=kind,
        confidence=round(min(1.0, max(0.0, confidence)), 3),
        long_scenario=long_s,
        short_scenario=short_s,
        reason=reason,
    )


__all__ = [
    "IndicatorPanel",
    "PinnedVerdict",
    "build_pinned_indicator_panel",
    "build_pinned_verdict",
    "is_pinned_symbol",
]

"""Signal strength — evidence-agreement scoring, not weighted-average conviction."""
from __future__ import annotations

from hunt_core.deep.verdict_v2._helpers import clamp01
from hunt_core.deep.verdict_v2.types import (
    DataQualityReport,
    DisagreementState,
    EngineOutput,
    ExpectedPath,
    HorizonForecast,
    ScenarioFragility,
    SignalStrength,
    TradePlan,
)

_COVERAGE_PENALTY_THRESHOLD = 0.55
_PRECIOUS_METAL_COVERAGE_THRESHOLD = 0.65
_PRECIOUS_METAL_BOOST_DIVISOR = 0.90

_CONVICTION_THRESHOLD = 0.08


def _geometry_confidence(plan: TradePlan | None) -> float:
    """Quality score for Entry/SL/TP — independent of scenario direction."""
    if plan is None:
        return 0.0
    rr_score = clamp01(plan.rr_tp1 / 3.0)  # 3:1 RR = full score
    level_score = clamp01(len(plan.level_sources) / 4.0)  # 4+ sources = full score
    lo, hi = plan.entry_zone
    mid = (lo + hi) / 2.0
    if mid > 0:
        zone_pct = abs(hi - lo) / mid
        zone_score = clamp01(1.0 - zone_pct / 0.02)  # >2% wide = 0, <0.5% tight = 1
    else:
        zone_score = 0.3
    return round(clamp01(rr_score * 0.50 + level_score * 0.30 + zone_score * 0.20), 3)


def _engine_agreement(
    engines: dict[str, EngineOutput],
    direction: str,
) -> tuple[float, float, dict[str, float]]:
    """Count how many engines agree on direction, weighted by information value.

    Returns (agreement_ratio, max_conviction, per-engine contrib).
    """
    agree_weight = 0.0
    disagree_weight = 0.0
    total_weight = 0.0
    max_conv = 0.0
    contrib: dict[str, float] = {}

    for name, eng in engines.items():
        w = max(eng.coverage_quality * eng.information_value, 0.01)
        total_weight += w

        eng_dir = "long" if eng.long > eng.short else "short" if eng.short > eng.long else "neutral"
        conv = eng.conviction

        if conv < _CONVICTION_THRESHOLD:
            contrib[name] = 0.0
            continue

        if eng_dir == direction:
            agree_weight += w * conv
            contrib[name] = round(conv, 3)
            max_conv = max(max_conv, conv)
        elif eng_dir != "neutral":
            disagree_weight += w * conv
            contrib[name] = round(-conv, 3)

    if total_weight <= 0:
        return 0.0, 0.0, contrib

    agreement = agree_weight / total_weight
    disagreement = disagree_weight / total_weight
    net = clamp01(agreement - disagreement * 0.5)
    return net, max_conv, contrib


def compute_signal_strength(
    path: ExpectedPath,
    horizons: dict[str, HorizonForecast],
    fragility: ScenarioFragility,
    disagree: DisagreementState,
    data: DataQualityReport,
    *,
    symbol: str = "",
    topology_kind: str = "",
    engines: dict[str, EngineOutput] | None = None,
    plan: TradePlan | None = None,
) -> SignalStrength:
    contrib: dict[str, float] = {}

    if engines and path.direction in ("long", "short"):
        agreement, max_conv, eng_contrib = _engine_agreement(engines, path.direction)
        base = clamp01(
            path.probability_rank * 0.30
            + agreement * 0.40
            + max_conv * 0.30
        )
        contrib["path"] = round(path.probability_rank * 0.30, 4)
        contrib["agreement"] = round(agreement * 0.40, 4)
        contrib["max_conv"] = round(max_conv * 0.30, 4)
        contrib.update({f"eng_{k}": v for k, v in eng_contrib.items()})
    else:
        b = horizons.get("B")
        base = path.probability_rank
        contrib["path"] = base
        if b:
            blended = clamp01(path.probability_rank * 0.40 + b.conviction * 0.60)
            contrib["horizon"] = round(blended - base, 4)
            base = blended

    topo_delta = 0.0
    if topology_kind == "aligned_trend":
        topo_delta = 0.10
    elif topology_kind in {"bull_pullback", "bear_rally"}:
        topo_delta = 0.06
    elif topology_kind == "compression":
        topo_delta = -0.05
    if topo_delta:
        contrib["topology"] = topo_delta
        base = clamp01(base + topo_delta)

    frag_penalty = -fragility.score * 0.15
    disagree_penalty = -disagree.score * 0.10
    contrib["fragility"] = round(frag_penalty, 4)
    contrib["disagree"] = round(disagree_penalty, 4)
    base = clamp01(base + frag_penalty + disagree_penalty)

    # scenario_confidence = analytical score before data quality cap
    scenario_conf = round(base, 3)

    capped = False
    if data.coverage_score < _COVERAGE_PENALTY_THRESHOLD:
        pre = base
        base *= data.coverage_score / _COVERAGE_PENALTY_THRESHOLD
        contrib["data_cap"] = round(base - pre, 4)
        capped = True
    if symbol.upper().startswith(("XAU", "XAG")) and data.coverage_score < _PRECIOUS_METAL_COVERAGE_THRESHOLD:
        pre = base
        if path.probability_rank >= 0.50:
            base = clamp01(base / _PRECIOUS_METAL_BOOST_DIVISOR)
        else:
            base *= 0.88
        contrib["precious_adj"] = round(base - pre, 4)
        capped = True

    if base >= 0.72:
        label = "strong"
    elif base >= 0.52:
        label = "moderate"
    else:
        label = "weak"
    return SignalStrength(
        score=round(base, 3),
        label=label,  # type: ignore[arg-type]
        capped_by_data=capped,
        breakdown=contrib,
        scenario_confidence=scenario_conf,
        geometry_confidence=_geometry_confidence(plan),
    )


def apply_reconcile_to_strength(
    strength: SignalStrength,
    reconcile: "ReconciliationResult",
) -> SignalStrength:

    if reconcile.level == "coherent" or reconcile.strength_multiplier >= 1.0:
        return strength
    score = clamp01(strength.score * reconcile.strength_multiplier)
    if score >= 0.72:
        label = "strong"
    elif score >= 0.52:
        label = "moderate"
    else:
        label = "weak"
    bd = dict(strength.breakdown)
    if reconcile.strength_multiplier < 1.0:
        bd["reconcile"] = round(score - strength.score, 4)
    return SignalStrength(
        score=round(score, 3),
        label=label,  # type: ignore[arg-type]
        capped_by_data=strength.capped_by_data,
        breakdown=bd,
        scenario_confidence=strength.scenario_confidence,
        geometry_confidence=strength.geometry_confidence,
    )

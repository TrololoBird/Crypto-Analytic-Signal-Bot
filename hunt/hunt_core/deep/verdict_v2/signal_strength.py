"""Signal strength — rank only, not P(win)."""
from __future__ import annotations

from hunt_core.deep.verdict_v2._helpers import clamp01
from hunt_core.deep.verdict_v2.types import (
    DataQualityReport,
    DisagreementState,
    ExpectedPath,
    HorizonForecast,
    ScenarioFragility,
    SignalStrength,
)


def compute_signal_strength(
    path: ExpectedPath,
    horizons: dict[str, HorizonForecast],
    fragility: ScenarioFragility,
    disagree: DisagreementState,
    data: DataQualityReport,
    *,
    symbol: str = "",
    topology_kind: str = "",
) -> SignalStrength:
    b = horizons.get("B")
    base = path.probability_rank
    if b:
        base = clamp01(path.probability_rank * 0.40 + b.conviction * 0.60)
    if topology_kind == "aligned_trend":
        base = clamp01(base + 0.07)
    elif topology_kind in {"bull_pullback", "bear_rally"}:
        base = clamp01(base + 0.04)
    elif topology_kind == "compression":
        base = clamp01(base - 0.03)
    base = clamp01(base - fragility.score * 0.12 - disagree.score * 0.08)
    capped = False
    if data.coverage_score < 0.55:
        base *= data.coverage_score / 0.55
        capped = True
    if symbol.upper().startswith(("XAU", "XAG")) and data.coverage_score < 0.65:
        if path.probability_rank >= 0.50:
            base = clamp01(base / 0.90)
        else:
            base *= 0.88
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
    )


def apply_reconcile_to_strength(
    strength: SignalStrength,
    reconcile: "ReconciliationResult",
) -> SignalStrength:
    from hunt_core.deep.verdict_v2.reconcile import ReconciliationResult

    if reconcile.level == "coherent" or reconcile.strength_multiplier >= 1.0:
        return strength
    score = clamp01(strength.score * reconcile.strength_multiplier)
    if score >= 0.72:
        label = "strong"
    elif score >= 0.52:
        label = "moderate"
    else:
        label = "weak"
    return SignalStrength(
        score=round(score, 3),
        label=label,  # type: ignore[arg-type]
        capped_by_data=strength.capped_by_data,
    )

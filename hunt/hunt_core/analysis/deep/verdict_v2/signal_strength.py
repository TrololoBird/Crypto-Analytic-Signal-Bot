"""Signal strength — rank only, not P(win)."""
from __future__ import annotations

from hunt_core.analysis.deep.verdict_v2._helpers import clamp01
from hunt_core.analysis.deep.verdict_v2.types import (
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

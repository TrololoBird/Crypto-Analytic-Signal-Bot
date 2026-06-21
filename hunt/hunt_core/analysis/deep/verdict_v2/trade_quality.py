"""Trade quality — RR advisory, never kills scenario."""
from __future__ import annotations

from hunt_core.analysis.deep.verdict_v2._helpers import clamp01
from hunt_core.analysis.deep.verdict_v2.config import VerdictV2Config
from hunt_core.analysis.deep.verdict_v2.types import TradePlan, TradeQuality


def compute_trade_quality(plan: TradePlan | None, cfg: VerdictV2Config) -> TradeQuality:
    if plan is None:
        return TradeQuality(
            score=0.0,
            rr_nearest=0.0,
            rr_stretch=0.0,
            verdict="poor",
            advisory="No trade plan — WAIT advisory",
        )
    rr1 = plan.rr_tp1
    rr3 = plan.rr_tp3
    score = clamp01(rr1 / 2.5)
    if rr1 >= cfg.trade_rr_favorable:
        verdict = "favorable"
        advisory = f"RR TP1 {rr1:.1f}R favorable"
    elif rr1 >= cfg.trade_rr_poor:
        verdict = "marginal"
        advisory = f"RR TP1 {rr1:.1f}R marginal — size down"
    else:
        verdict = "poor"
        advisory = f"RR TP1 {rr1:.1f}R poor — advisory only"
    return TradeQuality(
        score=round(score, 3),
        rr_nearest=round(rr1, 2),
        rr_stretch=round(rr3, 2),
        verdict=verdict,  # type: ignore[arg-type]
        advisory=advisory,
    )

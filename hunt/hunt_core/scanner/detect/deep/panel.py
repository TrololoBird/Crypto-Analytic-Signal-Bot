"""Deep factor panel — the full calibrated read for one symbol, ungated.

Same factors and fusion as the watch path, but the phase is reported rather than used
to shut a gate: deep analysis is an explanation, not a delivery decision. Each active
factor is given a short plain-language interpretation for the human reader.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from hunt_core.scanner.detect import fusion as Fz
from hunt_core.scanner.detect import phase as Ph
from hunt_core.scanner.detect.factors import AMPLIFIER, FactorScore, compute_factors
from hunt_core.scanner.detect.windows import FeatureWindow

# Plain-language reading of each factor's signed score (sign already oriented
# long-positive in factors.py). Amplifiers describe magnitude, not side.
_DIRECTIONAL_WORDS = {
    "book": ("bids stacking (buy-side book)", "asks stacking (sell-side book)"),
    "structure": ("oversold / basing", "overbought / extended"),
    "funding": ("funding favours longs (shorts crowded)", "funding favours shorts (longs crowded)"),
    "flow": ("net taker buying", "net taker selling"),
}
_AMPLIFIER_WORDS = {
    "oi_pressure": "open-interest moving fast (real positioning)",
    "compression": "volatility coiled (energy building)",
}


@dataclass(frozen=True)
class FactorReading:
    name: str
    kind: str
    score: float
    text: str


@dataclass(frozen=True)
class DeepPanel:
    symbol: str
    tf: str
    price: float | None
    fusion: Fz.FusionScore
    phase: Ph.PhaseInfo
    readings: list[FactorReading] = field(default_factory=list)

    @property
    def side(self) -> str:
        return self.fusion.side

    @property
    def confidence(self) -> float:
        return self.fusion.confidence


def _interpret(f: FactorScore) -> str:
    if not f.active:
        return f"{f.name}: n/a ({f.detail})"
    if f.kind == AMPLIFIER:
        return _AMPLIFIER_WORDS.get(f.name, f.name)
    pos, neg = _DIRECTIONAL_WORDS.get(f.name, (f"{f.name}+", f"{f.name}-"))
    return pos if f.score >= 0 else neg


def build_panel(window: FeatureWindow) -> DeepPanel:
    """Compute the ungated deep factor panel for the current bar."""
    factors = compute_factors(window)
    fusion = Fz.fuse(factors)
    phase = Ph.assess_phase(window, side=fusion.side)
    readings = [
        FactorReading(f.name, f.kind, round(f.score, 3), _interpret(f))
        for f in factors
        if f.active
    ]
    return DeepPanel(
        symbol=window.symbol,
        tf=window.tf,
        price=window.price,
        fusion=fusion,
        phase=phase,
        readings=readings,
    )


__all__ = ["DeepPanel", "FactorReading", "build_panel"]

"""Detection result + orchestrator — the single entry point of the fusion engine.

``build_detection`` runs the whole pipeline for one bar: factors → fuse → phase → gate,
then closes the gate whenever the phase is not a matching PRE window (MID continuation
can never deliver). ``Detection.to_setup_dict`` adapts the result into the
``row["dump" | "long" | "lifecycle"]`` shape the preserved delivery seam consumes; the
exact field mapping is finalized in phase-5 against the live consumer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from hunt_core.detect import calibrate as C
from hunt_core.detect import fusion as Fz
from hunt_core.detect import phase as Ph
from hunt_core.detect.factors import FactorScore, compute_factors
from hunt_core.detect.windows import FeatureWindow


@dataclass(frozen=True)
class Detection:
    """Fusion-engine verdict for one symbol at the current bar."""

    symbol: str
    tf: str
    side: str  # long | short | none
    phase: str  # pre_pump | pre_dump | mid | neutral
    watch_ok: bool  # phase is a PRE window matching side
    gate_open: bool  # final watch-delivery decision (gate AND watch_ok)
    confidence: float
    magnitude: float
    price: float | None
    fusion: Fz.FusionScore
    gate: Fz.GateDecision
    phase_info: Ph.PhaseInfo
    factors: list[FactorScore] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def active_factors(self) -> list[FactorScore]:
        return [f for f in self.factors if f.active]

    def to_setup_dict(self) -> dict[str, object]:
        """Adapt to the delivery-path row shape: {dump, long, lifecycle}.

        The matching side carries a setup dict (``confirmed`` == final gate); the other
        side is empty. ``lifecycle`` exposes the phase descriptor. Geometry (entry/SL/TP)
        is attached downstream by the preserved levels path — this adapter supplies the
        decision, side, confidence and phase only.
        """
        setup = {
            "direction": self.side,
            "confirmed": self.gate_open,
            "fusion_score": round(self.fusion.fusion_score, 1),
            "magnitude": round(self.magnitude, 4),
            "vol_adj_magnitude": round(self.gate.vol_adjusted_magnitude, 4),
            "z_dir": round(self.fusion.z_dir, 4),
            "amp": round(self.fusion.amp, 4),
            "n_active": self.fusion.n_active,
            "agreement": self.fusion.agreement,
            "phase": self.phase,
            "factors": {f.name: round(f.score, 4) for f in self.active_factors},
            "gate_reason": self.gate.reason,
            "gate_threshold": (round(self.gate.threshold, 4) if self.gate.threshold is not None else None),
            "reasons": list(self.reasons),
        }
        lifecycle = {
            "phase": self.phase,
            "bias": self.side,
            "watch_ok": self.watch_ok,
            "mid": self.phase_info.mid,
            "cusum": round(self.phase_info.cusum, 4),
            "band": (round(self.phase_info.band, 4) if self.phase_info.band is not None else None),
        }
        long_setup = setup if self.side == "long" else {}
        dump_setup = setup if self.side == "short" else {}
        return {"long": long_setup, "dump": dump_setup, "lifecycle": lifecycle}

    def as_summary(self) -> dict[str, object]:
        """Compact one-line dict for logging / replay output."""
        return {
            "symbol": self.symbol,
            "side": self.side,
            "phase": self.phase,
            "gate_open": self.gate_open,
            "fusion_score": round(self.fusion.fusion_score, 1),
            "magnitude": round(self.magnitude, 4),
            "n_active": self.fusion.n_active,
            "gate_reason": self.gate.reason,
        }


def build_detection(
    window: FeatureWindow,
    *,
    magnitude_history: pl.Series | None = None,
    q_gate: float = Fz.DEFAULT_Q_GATE,
    q_phase: float = Ph.DEFAULT_Q_PHASE,
    min_n: int = C.MIN_N_DEFAULT,
) -> Detection:
    """Run factors → fuse → phase → gate for one bar; MID closes the watch gate."""
    factors = compute_factors(window)
    fusion = Fz.fuse(factors)
    phase_info = Ph.assess_phase(window, side=fusion.side, q_phase=q_phase, min_n=min_n)
    gate_decision = Fz.gate(
        fusion,
        magnitude_history,
        q=q_gate,
        min_n=min_n,
        atr_pct=window.last("atr_pct"),
    )

    reasons: list[str] = [gate_decision.reason]
    gate_open = gate_decision.gate_open and phase_info.watch_ok
    if gate_decision.gate_open and not phase_info.watch_ok:
        reasons.append(f"phase_block:{phase_info.phase}")

    return Detection(
        symbol=window.symbol,
        tf=window.tf,
        side=fusion.side,
        phase=phase_info.phase,
        watch_ok=phase_info.watch_ok,
        gate_open=gate_open,
        confidence=fusion.fusion_score / 100.0,
        magnitude=fusion.magnitude,
        price=window.price,
        fusion=fusion,
        gate=gate_decision,
        phase_info=phase_info,
        factors=factors,
        reasons=reasons,
    )


__all__ = ["Detection", "build_detection"]

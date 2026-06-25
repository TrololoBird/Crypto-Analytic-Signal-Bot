"""Detection result + orchestrator — the single entry point of the fusion engine.

``build_detection`` runs the whole pipeline for one bar: factors → fuse → phase → gate,
then closes the gate whenever the phase is not a matching PRE window (MID continuation
can never deliver). ``Detection.to_setup_dict`` adapts the result into the
``row["dump" | "long" | "lifecycle"]`` shape the preserved delivery seam consumes; the
exact field mapping is finalized in phase-5 against the live consumer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl

from hunt_core.scanner.detect import fusion as Fz
from hunt_core.scanner.detect import phase as Ph
from hunt_core.scanner.detect.factors import FactorScore, compute_factors
from hunt_core.scanner.detect.windows import FeatureWindow


@dataclass(frozen=True)
class Detection:
    """Fusion-engine verdict for one symbol at the current bar."""

    symbol: str
    tf: str
    side: str  # long | short | none
    phase: str  # pre_pump | pre_dump | mid | neutral
    watch_ok: bool  # phase is a PRE window matching side
    gate_open: bool  # final watch-delivery decision (momentum gate AND watch_ok)
    pre_gate_open: bool  # structure-based gate for pre-phase (bypasses magnitude floor)
    signal_type: str  # "pre_phase" | "mid_phase" | "none"
    confidence: float
    magnitude: float
    price: float | None
    fusion: Fz.FusionScore
    gate: Fz.GateDecision
    pre_gate: Fz.PreGateDecision | None
    phase_info: Ph.PhaseInfo
    factors: list[FactorScore] = field(default_factory=list)
    quarantine_factors: list[FactorScore] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def active_factors(self) -> list[FactorScore]:
        return [f for f in self.factors if f.active]

    def to_setup_dict(self) -> dict[str, object]:
        """Adapt to the delivery-path row shape: {dump, long, lifecycle}."""
        setup = {
            "direction": self.side,
            "confirmed": self.gate_open or self.pre_gate_open,
            "fusion_score": round(self.fusion.fusion_score, 1),
            "magnitude": round(self.magnitude, 4),
            "vol_adj_magnitude": round(self.gate.vol_adjusted_magnitude, 4),
            "z_dir": round(self.fusion.z_dir, 4),
            "amp": round(self.fusion.amp, 4),
            "n_active": self.fusion.n_active,
            "agreement": self.fusion.agreement,
            "phase": self.phase,
            "signal_type": self.signal_type,
            "factors": {f.name: round(f.score, 4) for f in self.active_factors},
            "quarantine_factors": {
                f.name: round(f.score, 4) for f in self.quarantine_factors if f.active
            },
            "gate_reason": self.gate.reason,
            "gate_threshold": (round(self.gate.threshold, 4) if self.gate.threshold is not None else None),
            "reasons": list(self.reasons),
        }
        if self.pre_gate is not None:
            setup["pre_gate"] = {
                "open": self.pre_gate.pre_gate_open,
                "energy_hits": self.pre_gate.energy_hits,
                "structure_score": round(self.pre_gate.structure_score, 3),
                "reason": self.pre_gate.reason,
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
            "pre_gate_open": self.pre_gate_open,
            "signal_type": self.signal_type,
            "fusion_score": round(self.fusion.fusion_score, 1),
            "magnitude": round(self.magnitude, 4),
            "n_active": self.fusion.n_active,
            "gate_reason": self.gate.reason,
        }


def build_detection(
    window: FeatureWindow,
    *,
    magnitude_history: pl.Series | None = None,
    q_gate: float | None = None,
    q_phase: float | None = None,
    min_n: int | None = None,
    context: dict[str, Any] | None = None,
) -> Detection:
    """Run factors → fuse → phase → gate for one bar; MID closes the watch gate.

    Dual-gate architecture:
    - GateDecision (momentum-based) for mid-phase signals
    - PreGateDecision (structure-based) for pre_pump/pre_dump/coil signals
    """
    from hunt_core.scanner.detect.factors_quarantine import compute_quarantine_factors

    factors = compute_factors(window, row=context)
    quarantine = compute_quarantine_factors(window, row=context)
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

    # High energy + undecided side → coil bracket (ARMED), not mission-blocked.
    coil_armed = (
        fusion.side == "none"
        and phase_info.phase == "coil"
        and fusion.magnitude >= 0.5
        and fusion.n_active >= 2
    )
    if coil_armed:
        gate_open = True
        reasons.append("coil_bracket_armed")
    if gate_decision.gate_open and not phase_info.watch_ok and not coil_armed:
        reasons.append(f"phase_block:{phase_info.phase}")

    # P5: preparation readiness opens the gate when fusion magnitude/phase blocked
    # but energy+direction resolve (fixes below_abs_floor + mid on violent alts).
    ctx = context if isinstance(context, dict) else {}
    prep_ready = False
    prep_tags: list[str] = []
    if ctx and fusion.side in {"long", "short"}:
        from hunt_core.scanner.gate._mission import assess_preparation_readiness

        prep_ready, prep_tags = assess_preparation_readiness(ctx, direction=fusion.side)
        if prep_ready and not gate_open:
            if phase_info.mid:
                reasons.append("prep_blocked_mid_leg")
            else:
                gate_open = True
                reasons.append(f"prep_readiness:{','.join(prep_tags) or 'ok'}")

    # Dual-gate: structure-based gate for pre-phase signals
    pre_gate_decision: Fz.PreGateDecision | None = None
    pre_gate_open = False
    signal_type = "none"
    is_pre = phase_info.phase in {"pre_pump", "pre_dump", "coil"}
    if is_pre and ctx and fusion.side in {"long", "short"}:
        energy_hits = len(prep_tags)
        # structure_score uses raw book imbalance (independent of fusion z_dir)
        market_ctx = ctx.get("market") if isinstance(ctx.get("market"), dict) else {}
        book_imb = float(
            market_ctx.get("ws_depth_imbalance")
            or market_ctx.get("depth_imbalance")
            or market_ctx.get("map_book_imbalance_1pct")
            or 0
        )
        structure_score = abs(book_imb)
        pre_gate_decision = Fz.pre_phase_gate(
            energy_hits=energy_hits,
            structure_score=structure_score,
            magnitude=fusion.magnitude,
        )
        pre_gate_open = pre_gate_decision.pre_gate_open
        reasons.append(f"pre_gate:{pre_gate_decision.reason}")

        if pre_gate_open:
            signal_type = "pre_phase"
            gate_open = True
        else:
            signal_type = "pre_phase_blocked"
    elif phase_info.mid:
        if gate_open or coil_armed:
            signal_type = "mid_phase"
        else:
            signal_type = "mid_phase_blocked"

    return Detection(
        symbol=window.symbol,
        tf=window.tf,
        side=fusion.side,
        phase=phase_info.phase,
        watch_ok=phase_info.watch_ok,
        gate_open=gate_open,
        pre_gate_open=pre_gate_open,
        signal_type=signal_type,
        confidence=fusion.fusion_score / 100.0,
        magnitude=fusion.magnitude,
        price=window.price,
        fusion=fusion,
        gate=gate_decision,
        pre_gate=pre_gate_decision,
        phase_info=phase_info,
        factors=factors,
        quarantine_factors=quarantine,
        reasons=reasons,
    )


__all__ = ["Detection", "build_detection"]

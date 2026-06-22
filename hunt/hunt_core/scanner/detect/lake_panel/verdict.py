"""Deep verdict — a structural, human-readable conclusion (not a delivery gate).

The verdict is decided by structure, not by tuned score cutoffs: no actionable side,
a move already underway (MID → late), internal disagreement (mixed), or a clean PRE
window matching the lean. Only the *strength word* uses presentation bands on the
fused confidence — these label the readout for a human, they do not gate anything.
"""
from __future__ import annotations

from dataclasses import dataclass

from hunt_core.scanner.detect import phase as Ph
from hunt_core.scanner.detect.lake_panel.panel import DeepPanel
from hunt_core.scanner.detect.fusion import MIN_ACTIVE_DIRECTIONAL

# Presentation-only confidence bands for the strength word (display, not a gate).
_STRONG = 0.75
_MODERATE = 0.60


def _strength_word(confidence: float) -> str:
    if confidence >= _STRONG:
        return "strong"
    if confidence >= _MODERATE:
        return "moderate"
    return "weak"


@dataclass(frozen=True)
class Verdict:
    stance: str  # neutral | late | mixed | pre_pump | pre_dump
    headline: str
    rationale: str
    actionable: bool


def build_verdict(panel: DeepPanel) -> Verdict:
    f = panel.fusion
    if f.side == "none" or f.n_active < MIN_ACTIVE_DIRECTIONAL:
        return Verdict(
            "neutral",
            "No actionable read",
            f"only {f.n_active} active directional factor(s); below the floor of "
            f"{MIN_ACTIVE_DIRECTIONAL}.",
            False,
        )
    if panel.phase.phase == Ph.MID:
        return Verdict(
            "late",
            "Move already underway",
            "CUSUM is past the symbol's activation band — this is a continuation, not a "
            "pre-move; entering here carries late-chase risk.",
            False,
        )
    if not f.agreement:
        return Verdict(
            "mixed",
            "Mixed signals",
            "the directional aggregate disagrees with the factor rank vote; no clean side.",
            False,
        )
    strength = _strength_word(f.confidence)
    side_word = "pre-pump" if f.side == "long" else "pre-dump"
    stance = Ph.PRE_PUMP if f.side == "long" else Ph.PRE_DUMP
    return Verdict(
        stance,
        f"{strength.capitalize()} {side_word} forming",
        f"{f.n_active} directional factors agree on {f.side}; fused confidence "
        f"{f.confidence:.0%}; phase '{panel.phase.phase}'.",
        True,
    )


__all__ = ["Verdict", "build_verdict"]

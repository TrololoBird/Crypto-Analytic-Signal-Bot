"""SignalExplanation — structured explanation of why a signal exists.

Carried through the pipeline instead of being built ad-hoc at format time.
Enables the Telegram card to present a clear *why* before the *what*.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SignalExplanation:
    """Structured, human-readable explanation of a signal's origin.

    Built once during detection and carried through the delivery pipeline.
    The Telegram formatter reads this instead of re-deriving reasons from raw fields.
    """

    # ── Thesis — one-liner why this matters ──────────────────────────────────
    thesis: str = ""
    catalyst: str = ""

    # ── Triggers — machine-readable codes that fired ─────────────────────────
    triggers: tuple[str, ...] = field(default_factory=tuple)

    # ── Reasons — human-readable bullet points ───────────────────────────────
    reasons: tuple[str, ...] = field(default_factory=tuple)

    # ── Context — supporting market evidence ─────────────────────────────────
    confluence_count: int = 0
    for_reasons: tuple[str, ...] = field(default_factory=tuple)
    against_reasons: tuple[str, ...] = field(default_factory=tuple)

    # ── Phase & lifecycle ────────────────────────────────────────────────────
    phase: str = ""
    signal_type: str = ""

    # ── Risk context ─────────────────────────────────────────────────────────
    risk_bits: tuple[str, ...] = field(default_factory=tuple)
    invalidation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis": self.thesis,
            "catalyst": self.catalyst,
            "triggers": list(self.triggers),
            "reasons": list(self.reasons),
            "confluence_count": self.confluence_count,
            "for_reasons": list(self.for_reasons),
            "against_reasons": list(self.against_reasons),
            "phase": self.phase,
            "signal_type": self.signal_type,
            "risk_bits": list(self.risk_bits),
            "invalidation": self.invalidation,
        }


__all__ = ["SignalExplanation"]

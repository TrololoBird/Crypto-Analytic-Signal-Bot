"""Per-symbol state machine for manipulation pattern tracking.

Each symbol tracked by the scanner has a ``SymbolState`` instance that records
which pattern steps have been detected and at what score. State is updated on
every REST context cycle (every 5-15 min) by re-running detection primitives
on fresh OHLCV data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Direction = Literal["long", "short"]
PatternType = Literal["A", "A2", "A3", "B"]


@dataclass
class Bokovik:
    lo: float
    hi: float
    touches: int
    atr_ratio: float
    width_pct: float
    tf: str


@dataclass
class Sweep:
    extreme: float
    level_breached: float
    direction: str  # "below" | "above"
    tf: str
    ts: float = 0.0


@dataclass
class SymbolState:
    symbol: str
    pattern_type: PatternType | None = None
    score: float = 0.0
    steps_covered: int = 0
    total_steps: int = 0

    impulse_detected: bool = False
    absorption_detected: bool = False
    one_candle_absorb: bool = False
    bokoviks: list[Bokovik] = field(default_factory=list)
    sweep: Sweep | None = None
    structure_broken: bool = False
    candle_fade: bool = False
    instant_rejection: bool = False
    ltf_confirmed: bool = False

    macro_extreme: float = 0.0
    macro_tf: str = "1d"
    meso_tf: str = "4h"
    entry_ref: float | None = None
    target: float | None = None
    evidence: list[str] = field(default_factory=list)
    last_check: float = 0.0
    ws_subscribed: bool = False


def reset_state(symbol: str) -> SymbolState:
    return SymbolState(symbol=symbol)

"""Scanner detection — manipulation patterns (Pattern A: long, Pattern B: short).

The Scanner has exactly one signal-generation path: ``patterns.detect_manipulation_setup``.
All TA computed via Polars + polars_ta — zero manual Python loops for math.

Low-level primitives in ``events.py`` (Polars-first), state machine in ``patterns.py``,
score in ``scoring.py``, per-symbol state in ``state.py``.
"""
from __future__ import annotations

from hunt_core.scanner.detect.patterns import (
    Direction, ManipulationSetup, detect_manipulation_setup,
)
from hunt_core.scanner.detect.events import ohlcv_to_df, compute_features, atr
from hunt_core.scanner.detect.state import SymbolState
from hunt_core.scanner.detect.scoring import compute_score_a, compute_score_b

__all__ = [
    "Direction", "ManipulationSetup",
    "detect_manipulation_setup",
    "SymbolState",
    "ohlcv_to_df", "compute_features", "atr",
    "compute_score_a", "compute_score_b",
]

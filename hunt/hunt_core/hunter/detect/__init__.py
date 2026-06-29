"""Statistical fusion detection engine (replaces scan/* + regime FSM + gate/*).

Single self-calibrating pre-pump / pre-dump detector: every threshold is derived
from each symbol's own recent distribution (rolling quantile, robust z via
median/MAD, CUSUM change-point, regression slope) — no magic constants.

Public surface:
- ``calibrate`` primitives + ``windows`` builder (phase-1).
- ``factors`` — six normalized pre-move pressures (phase-2).
- ``fusion`` / ``phase`` + ``build_detection`` / ``Detection`` (phase-3).
- ``intra_bar_state`` — sub-15m WS-tick PRE detection (stage-1).
"""
from __future__ import annotations

from hunt_core.hunter.detect import calibrate, factors, fusion, phase, windows
from hunt_core.hunter.detect.intra_bar_state import (
    IntraBarConfig,
    IntraBarSignal,
    IntraBarState,
    intra_bar_config,
)
from hunt_core.hunter.detect.result import Detection, build_detection

__all__ = [
    "Detection",
    "IntraBarConfig",
    "IntraBarSignal",
    "IntraBarState",
    "build_detection",
    "calibrate",
    "factors",
    "fusion",
    "intra_bar_config",
    "phase",
    "windows",
]

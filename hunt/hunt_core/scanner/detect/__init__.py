"""Statistical fusion detection engine (replaces scan/* + regime FSM + gate/*).

Single self-calibrating pre-pump / pre-dump detector: every threshold is derived
from each symbol's own recent distribution (rolling quantile, robust z via
median/MAD, CUSUM change-point, regression slope) — no magic constants.

Public surface:
- ``calibrate`` primitives + ``windows`` builder (phase-1).
- ``factors`` — six normalized pre-move pressures (phase-2).
- ``fusion`` / ``phase`` + ``build_detection`` / ``Detection`` (phase-3).
"""
from __future__ import annotations

from hunt_core.scanner.detect import calibrate, factors, fusion, phase, windows
from hunt_core.scanner.detect.result import Detection, build_detection

__all__ = [
    "Detection",
    "build_detection",
    "calibrate",
    "factors",
    "fusion",
    "phase",
    "windows",
]

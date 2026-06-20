"""Independent deep-analysis path on the fusion factor layer.

Reuses the same calibrated factors as the watch detector but is **decoupled from the
watch gate**: it runs on any symbol in any phase (no MID block, no gate requirement),
producing a rich human-readable read — directional lean, per-factor breakdown,
ATR-scaled forecast scenarios, and a verdict — for ``/signal <SYMBOL>`` and pinned
symbols. Analysis only; it never delivers a signal.
"""
from __future__ import annotations

from hunt_core.detect.deep.report import DeepReport, build_deep_report, build_deep_report_from_lake

__all__ = ["DeepReport", "build_deep_report", "build_deep_report_from_lake"]

"""Offline/dev fusion deep reports — NOT for operator Telegram.

Operator TG and ``/signal`` use ``hunt_core.analysis.deep`` (Module 2).
This package reuses watch fusion factors for regression and ``_dev`` replay only.
"""
from __future__ import annotations

from hunt_core.detect.deep.report import DeepReport, build_deep_report, build_deep_report_from_lake

__all__ = ["DeepReport", "build_deep_report", "build_deep_report_from_lake"]

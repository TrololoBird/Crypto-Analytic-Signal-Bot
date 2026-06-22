"""Offline lake-panel reports for ``_dev/check_deep`` — NOT Module 1 Deep.

Operator ``/signal`` and pinned TG use ``hunt_core.deep`` (verdict_v2).
This package replays fusion factors over the parquet lake for regression smoke only.
"""
from __future__ import annotations

from hunt_core.scanner.detect.lake_panel.report import DeepReport, build_deep_report, build_deep_report_from_lake

__all__ = ["DeepReport", "build_deep_report", "build_deep_report_from_lake"]

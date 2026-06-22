"""Deprecated shim — import from hunt_core.deep instead."""
from hunt_core.deep.build import DeepAnalysis, build_deep_analysis, build_deep_report
from hunt_core.deep.verdict_v2.orchestrator import build_scenario_verdict

__all__ = ["DeepAnalysis", "build_deep_analysis", "build_deep_report", "build_scenario_verdict"]

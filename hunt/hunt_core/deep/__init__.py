"""Module 1 Deep — Verdict V2, pinned loop, scenario delivery."""
from hunt_core.deep.build import DeepAnalysis, build_deep_analysis, build_deep_report
from hunt_core.deep.engine import is_pinned_symbol
from hunt_core.deep.signal import btc_market_context
from hunt_core.deep.verdict_v2.orchestrator import build_scenario_verdict

MODULE_ID = 1
MODULE_NAME = "deep"

__all__ = [
    "MODULE_ID",
    "MODULE_NAME",
    "DeepAnalysis",
    "btc_market_context",
    "build_deep_analysis",
    "build_deep_report",
    "build_scenario_verdict",
    "is_pinned_symbol",
]

"""Inert stubs for legacy deep_signal / pinned_deep / catalog formatting helpers.

The legacy detection+formatting modules are removed; these no-ops let the query and
telegram formatting paths import cleanly and degrade gracefully (optional scenario,
verdict, and indicator-panel blocks simply render empty). The fusion deep path
(``detect.deep.build_deep_report``) is the supported deep analysis.
"""
from __future__ import annotations

from typing import Any


# --- scenario builders / formatters (deep_signal) ---------------------------
def build_liquidity_scenarios(*_a: Any, **_k: Any) -> None:
    return None


def format_liquidity_scenarios_telegram(*_a: Any, **_k: Any) -> str:
    return ""


def build_poc_level_scenarios(*_a: Any, **_k: Any) -> None:
    return None


def format_poc_level_scenarios_telegram(*_a: Any, **_k: Any) -> str:
    return ""


def stamp_fusion_on_row(*_a: Any, **_k: Any) -> None:
    return None


# --- pinned-deep verdict / panels -------------------------------------------
class PinnedVerdict:  # inert placeholder for isinstance checks
    pass


def build_pinned_verdict(*_a: Any, **_k: Any) -> None:
    return None


def build_pinned_indicator_panel(*_a: Any, **_k: Any) -> None:
    return None


# --- legacy anticipation delivery (scan.early) ------------------------------
def prepare_anticipation_delivery(*_a: Any, **_k: Any) -> None:
    return None


def format_liquidation_burst_advisory(burst: Any) -> str:
    return ""


def format_ignition_telegram(ignition: Any) -> str:
    return ""


__all__ = [
    "PinnedVerdict",
    "build_liquidity_scenarios",
    "build_pinned_indicator_panel",
    "build_pinned_verdict",
    "build_poc_level_scenarios",
    "format_ignition_telegram",
    "format_liquidity_scenarios_telegram",
    "format_poc_level_scenarios_telegram",
    "prepare_anticipation_delivery",
    "stamp_fusion_on_row",
]

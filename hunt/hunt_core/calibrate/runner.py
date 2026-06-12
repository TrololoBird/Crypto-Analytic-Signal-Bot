"""Unified calibration runner — merges calibration modules."""

from __future__ import annotations

import json
from typing import Any

from hunt_watch.calibration import (
    BACKTEST_SL_GATE,
    compute_auto_calibration,
    compute_backtest_rates,
    compute_gate_edge,
    early_exit_verdict,
)
from hunt_watch.param_calibration import run_full_calibration as run_param_full
from hunt_watch.paths import SIGNAL_STATE


def run_full_calibration(
    *,
    fetch_rest: bool = True,
    backfill: bool = True,
    rest_symbol_limit: int = 40,
    include_auto: bool = True,
) -> dict[str, Any]:
    """Run all calibration paths; returns combined report (suggestions only)."""
    out: dict[str, Any] = {
        "backtest": compute_backtest_rates(),
        "gate_edge": compute_gate_edge(),
        "early_exit": early_exit_verdict(),
        "backtest_sl_gate": BACKTEST_SL_GATE,
    }
    if include_auto and SIGNAL_STATE.exists():
        try:
            state = json.loads(SIGNAL_STATE.read_text(encoding="utf-8"))
            auto = compute_auto_calibration(state)
            out["auto"] = auto
            out["safe_to_apply"] = auto.get("safe_to_apply", False)
        except (OSError, json.JSONDecodeError):
            out["auto"] = {}
            out["safe_to_apply"] = False
    out["param_full"] = run_param_full(
        fetch_rest=fetch_rest,
        backfill=backfill,
        rest_symbol_limit=rest_symbol_limit,
    )
    return out

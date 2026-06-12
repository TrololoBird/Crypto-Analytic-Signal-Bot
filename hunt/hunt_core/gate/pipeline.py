"""Unified gate pipeline — transparent reject-reason layer."""

from __future__ import annotations

from typing import Any

from hunt_watch.alert_explain import GateResult, evaluate_alert_gate
from hunt_watch.deliver.sniper import SniperConfig, sniper_block_reason
from hunt_core.gate.edge_policy import direction_block_reason


def run_gate_pipeline(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult:
    """Run H-B edge policy, optional sniper slice, then legacy alert gate."""
    edge_block = direction_block_reason(direction)
    if edge_block:
        return GateResult(ok=False, code=edge_block, message=edge_block)
    sniper = sniper_block_reason(
        direction=direction,
        setup=setup,
        row=row,
        lifecycle=lifecycle,
        config=sniper_config or SniperConfig.from_env(),
    )
    if sniper:
        return GateResult(ok=False, code=sniper, message=sniper)
    sym = symbol or str(row.get("symbol", ""))
    return evaluate_alert_gate(
        setup,
        direction=direction,
        symbol=sym,
        lifecycle=lifecycle,
        row=row,
    )

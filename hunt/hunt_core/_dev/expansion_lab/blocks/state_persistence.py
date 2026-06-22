"""Block 16 — State persistence (duration of the coiled state).

``compression = 0.9`` for one bar is weak; ``compression > 0.8`` for many consecutive
bars is a loaded spring. Reads the history ring buffer for consecutive dwell time.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import clamp01
from hunt_core._dev.expansion_lab.blocks._common import abstain, result
from hunt_core._dev.expansion_lab.config import ExpansionConfig
from hunt_core._dev.expansion_lab.history import ExpansionHistory
from hunt_core._dev.expansion_lab.types import BlockContext, BlockResult

NAME = "state_persistence"

_FULL_DWELL = 12  # bars at/above threshold to saturate the score


def score(
    ctx: BlockContext,
    *,
    history: ExpansionHistory,
    cfg: ExpansionConfig,
) -> BlockResult:
    thr = cfg.persistence_threshold
    comp_dwell = history.persistence_count(ctx.symbol, "compression", threshold=thr)
    fuel_dwell = history.persistence_count(ctx.symbol, "fuel_imbalance", threshold=thr * 0.75)
    dwell = max(comp_dwell, fuel_dwell)
    if dwell <= 1:
        return abstain(NAME)
    sval = clamp01(dwell / _FULL_DWELL)
    evidence: list[str] = []
    if comp_dwell >= 2:
        evidence.append(f"coil×{comp_dwell}bars")
    if fuel_dwell >= 2:
        evidence.append(f"fuel×{fuel_dwell}bars")
    return result(NAME, sval, direction="up", evidence=tuple(evidence))


__all__ = ["NAME", "score"]

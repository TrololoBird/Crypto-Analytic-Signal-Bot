"""Level selection for trade plan."""
from __future__ import annotations

from typing import Any

from hunt_core.deep.verdict_v2._helpers import atr_from_row, safe_float
from hunt_core.shared.primitives.targets import (
    collect_downward_targets as _collect_downward_targets,
    collect_upward_targets as _collect_upward_targets,
)

_STOP_BUFFER_ATR = 0.35


def pick_targets(row: dict[str, Any], direction: str) -> tuple[list[float], list[str]]:
    price = safe_float(row.get("price"))
    if price <= 0:
        return [], []
    if direction == "long":
        return _collect_upward_targets(row, price)
    return _collect_downward_targets(row, price)


def _structural_level(row: dict[str, Any], direction: str) -> float:
    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    pools = structure.get("liquidity_pools") if isinstance(structure.get("liquidity_pools"), dict) else {}
    key_levels = structure.get("key_levels") if isinstance(structure.get("key_levels"), dict) else {}
    if direction == "long":
        return safe_float(pools.get("nearest_below") or key_levels.get("support"))
    return safe_float(pools.get("nearest_above") or key_levels.get("resistance"))


def pick_stop(row: dict[str, Any], direction: str, entry: float) -> tuple[float, str]:
    atr = atr_from_row(row)
    buffer = max(atr * _STOP_BUFFER_ATR, entry * 0.0008)
    structural = _structural_level(row, direction)
    if direction == "long":
        if structural > 0 and structural < entry:
            return round(structural - buffer, 6), "structure_below_buf"
        return round(entry - atr * 1.5, 6), "atr_fallback"
    if structural > 0 and structural > entry:
        return round(structural + buffer, 6), "structure_above_buf"
    return round(entry + atr * 1.5, 6), "atr_fallback"


def stop_structural_buffer_atr(row: dict[str, Any], plan_direction: str, stop: float, entry: float) -> float:
    """Distance from stop to nearest structural level in ATR units (R10 fragility input)."""
    atr = atr_from_row(row)
    if atr <= 0 or stop <= 0 or entry <= 0:
        return 1.0
    structural = _structural_level(row, plan_direction)
    if structural <= 0:
        return 1.0
    if plan_direction == "long":
        gap = stop - structural
    else:
        gap = structural - stop
    if gap < 0:
        return 0.0
    return gap / atr


def entry_zone(row: dict[str, Any], direction: str, pad_atr: float) -> tuple[tuple[float, float], str]:
    price = safe_float(row.get("price"))
    atr = atr_from_row(row)
    pad = atr * pad_atr
    if direction == "long":
        return (round(price - pad, 6), round(price, 6)), "pullback_zone"
    return (round(price, 6), round(price + pad, 6)), "rally_zone"

"""Level selection for trade plan."""
from __future__ import annotations

from typing import Any

from hunt_core.deep.verdict_v2._helpers import atr_from_row, safe_float
from hunt_core.shared.primitives.targets import (
    collect_downward_targets as _collect_downward_targets,
    collect_upward_targets as _collect_upward_targets,
)


def pick_targets(row: dict[str, Any], direction: str) -> tuple[list[float], list[str]]:
    price = safe_float(row.get("price"))
    if price <= 0:
        return [], []
    if direction == "long":
        return _collect_upward_targets(row, price)
    return _collect_downward_targets(row, price)


def pick_stop(row: dict[str, Any], direction: str, entry: float) -> tuple[float, str]:
    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    pools = structure.get("liquidity_pools") if isinstance(structure.get("liquidity_pools"), dict) else {}
    atr = atr_from_row(row)
    if direction == "long":
        below = safe_float(pools.get("nearest_below"))
        if below > 0 and below < entry:
            return below, "liq_pool_below"
        return entry - atr * 1.5, "atr_fallback"
    above = safe_float(pools.get("nearest_above"))
    if above > 0 and above > entry:
        return above, "liq_pool_above"
    return entry + atr * 1.5, "atr_fallback"


def entry_zone(row: dict[str, Any], direction: str, pad_atr: float) -> tuple[tuple[float, float], str]:
    price = safe_float(row.get("price"))
    atr = atr_from_row(row)
    pad = atr * pad_atr
    if direction == "long":
        return (round(price - pad, 6), round(price, 6)), "pullback_zone"
    return (round(price, 6), round(price + pad, 6)), "rally_zone"

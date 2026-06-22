"""Strategy-neutral forecasting / levels / conviction toolkit."""
from __future__ import annotations

from typing import Any

from hunt_core.shared.primitives.prokol import detect_prokol
from hunt_core.shared.primitives.targets import collect_downward_targets, collect_upward_targets


def atr_pad(entry: float, atr: float, *, k: float = 0.35) -> tuple[float, float]:
    """Symmetric entry band from ATR fraction."""
    pad = max(abs(entry) * 1e-8, abs(atr) * k)
    return entry - pad, entry + pad


def conviction_from_z(z: float | None, *, cap: float = 100.0) -> float:
    if z is None:
        return 0.0
    return min(cap, abs(float(z)) * 12.0)


def forecast_band(price: float, atr: float, *, side: str, k: float = 1.5) -> tuple[float, float]:
    move = abs(atr) * k
    if side == "long":
        return price, price + move
    return price - move, price


__all__ = [
    "atr_pad",
    "collect_downward_targets",
    "collect_upward_targets",
    "conviction_from_z",
    "detect_prokol",
    "forecast_band",
]

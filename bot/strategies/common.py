"""Shared numeric helpers for strategy detectors."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import polars as pl


def as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    numeric = as_float(value, default=math.nan)
    return numeric if math.isfinite(numeric) else None


def first_finite(*values: object) -> float | None:
    for value in values:
        numeric = finite_or_none(value)
        if numeric is not None:
            return numeric
    return None


def last(frame: pl.DataFrame, column: str, default: float = 0.0) -> float:
    if frame.is_empty() or column not in frame.columns:
        return default
    return as_float(frame.item(-1, column), default)


def previous(frame: pl.DataFrame, column: str, default: float = 0.0) -> float:
    if frame.height < 2 or column not in frame.columns:
        return default
    return as_float(frame.item(-2, column), default)


def orderflow_supports_reversal(
    prepared: object,
    direction: str,
    *,
    min_delta_long: float = 0.49,
    max_delta_short: float = 0.51,
    max_adverse_depth: float = 0.08,
    max_adverse_micro: float = 0.08,
) -> tuple[bool, dict[str, float]]:
    """Confirm reversal direction with delta / book microstructure (ICT-style recovery)."""
    depth = finite_or_none(getattr(prepared, "depth_imbalance", None))
    micro = finite_or_none(getattr(prepared, "microprice_bias", None))
    delta_ratio: float | None = None
    work = getattr(prepared, "work_15m", None)
    if work is not None and not work.is_empty() and "delta_ratio" in work.columns:
        delta_ratio = finite_or_none(work.item(-1, "delta_ratio"))
    if delta_ratio is None:
        agg = finite_or_none(getattr(prepared, "agg_trade_delta_30s", None))
        if agg is not None:
            delta_ratio = 0.5 + max(-0.5, min(0.5, agg / 2.0))

    details: dict[str, float] = {}
    if delta_ratio is not None:
        details["delta_ratio"] = delta_ratio
    if depth is not None:
        details["depth_imbalance"] = depth
    if micro is not None:
        details["microprice_bias"] = micro

    if direction == "long":
        if delta_ratio is not None and delta_ratio < min_delta_long:
            return False, details
        if depth is not None and depth <= -max_adverse_depth:
            return False, details
        if micro is not None and micro <= -max_adverse_micro:
            return False, details
    else:
        if delta_ratio is not None and delta_ratio > max_delta_short:
            return False, details
        if depth is not None and depth >= max_adverse_depth:
            return False, details
        if micro is not None and micro >= max_adverse_micro:
            return False, details
    return True, details

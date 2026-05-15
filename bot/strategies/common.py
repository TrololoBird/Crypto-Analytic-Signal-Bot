"""Shared numeric helpers for strategy detectors."""

from __future__ import annotations

import math

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

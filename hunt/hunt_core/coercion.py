"""Shared runtime coercion helpers for strict mypy."""

from __future__ import annotations

import math


def as_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    if isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return default
        return numeric if math.isfinite(numeric) else default
    return default


def as_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def row_float(row: object, key: str, default: float = 0.0) -> float:
    if not isinstance(row, dict):
        return default
    return as_float(row.get(key), default=default)

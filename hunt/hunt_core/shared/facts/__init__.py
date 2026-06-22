"""Layer-0 raw facts accessors — tick row / prepared symbol slices."""
from __future__ import annotations

from typing import Any


def market_block(row: dict[str, Any]) -> dict[str, Any]:
    m = row.get("market") or row.get("positioning") or {}
    return dict(m) if isinstance(m, dict) else {}


def timeframe_closed(row: dict[str, Any], tf: str) -> dict[str, Any]:
    key = f"{tf}_closed" if not tf.endswith("_closed") else tf
    blk = (row.get("timeframes") or {}).get(key) or {}
    return dict(blk) if isinstance(blk, dict) else {}


__all__ = ["market_block", "timeframe_closed"]

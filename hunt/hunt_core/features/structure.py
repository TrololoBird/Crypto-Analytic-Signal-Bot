"""Swing structure primitives (§2.7 — pivots + chart patterns)."""
from __future__ import annotations

from typing import Any

import polars as pl

from hunt_core.features.chart_patterns import chart_pattern_snapshot
from hunt_core.features.pivots import (
    _pivot_rows,
    rsi_trendline_break,
    with_spec_columns,
)


def structure_snapshot(df: pl.DataFrame, *, idx: int = -1) -> dict[str, Any]:
    """Merged pivot + chart-pattern structure block for TF snapshots."""
    if df.is_empty():
        return {"pivots": [], "chart": chart_pattern_snapshot(df)}
    pivots = _pivot_rows(df, idx=idx)
    end = df.height + idx + 1 if idx < 0 else idx + 1
    chart = chart_pattern_snapshot(df.slice(0, max(1, end)))
    return {"pivots": pivots, "chart": chart}


__all__ = [
    "_pivot_rows",
    "chart_pattern_snapshot",
    "rsi_trendline_break",
    "structure_snapshot",
    "with_spec_columns",
]

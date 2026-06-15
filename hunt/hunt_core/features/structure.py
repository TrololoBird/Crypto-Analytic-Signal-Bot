"""Swing structure + PP break detection (§2.7 / §H)."""
from __future__ import annotations

from typing import Any, Literal

import polars as pl

from hunt_core.features.chart_patterns import chart_pattern_snapshot
from hunt_core.features.pivots import (
    _pivot_rows,
    rsi_trendline_break,
    with_spec_columns,
)
from hunt_core.features.prepare import _swing_points

_SWING_N = 3
_TRUE_BODIES_MIN = 2
_EARLY_BODIES = 1
_MAX_PIVOT_AGE = 96


def _wick_zone(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    idx: int,
    *,
    side: Literal["high", "low"],
) -> tuple[float, float]:
    o, h, l, c = opens[idx], highs[idx], lows[idx], closes[idx]
    body_top = max(o, c)
    body_bot = min(o, c)
    if side == "high":
        return body_top, h
    return l, body_bot


def _bodies_beyond(
    opens: list[float],
    closes: list[float],
    *,
    start_idx: int,
    direction: Literal["below", "above"],
    level: float,
) -> int:
    count = 0
    for i in range(len(closes) - 1, start_idx, -1):
        body_top = max(opens[i], closes[i])
        body_bot = min(opens[i], closes[i])
        if direction == "below":
            if body_top < level:
                count += 1
            else:
                break
        elif body_bot > level:
            count += 1
        else:
            break
    return count


def _pp_side(
    work: pl.DataFrame,
    mask: pl.Series,
    *,
    side: Literal["high", "low"],
    closed: bool,
) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "pp_short_true": False,
        "pp_short_early": False,
        "pp_long_true": False,
        "pp_long_early": False,
    }
    if work.is_empty():
        return empty

    end = work.height - (2 if closed and work.height >= 2 else 1)
    if end < _SWING_N + 2:
        return empty

    opens = [float(x) for x in work["open"].to_list()]
    highs = [float(x) for x in work["high"].to_list()]
    lows = [float(x) for x in work["low"].to_list()]
    closes = [float(x) for x in work["close"].to_list()]
    swing_mask = mask.to_list()

    pivot_idx: int | None = None
    for i in range(end - 1, max(_SWING_N, end - _MAX_PIVOT_AGE) - 1, -1):
        if i < len(swing_mask) and swing_mask[i]:
            pivot_idx = i
            break
    if pivot_idx is None:
        return empty

    zone_lo, zone_hi = _wick_zone(opens, highs, lows, closes, pivot_idx, side=side)
    if side == "high":
        bodies = _bodies_beyond(
            opens,
            closes,
            start_idx=pivot_idx,
            direction="below",
            level=zone_lo,
        )
        return {
            "pp_short_true": bodies >= _TRUE_BODIES_MIN,
            "pp_short_early": bodies == _EARLY_BODIES,
            "pp_long_true": False,
            "pp_long_early": False,
            "pp_short_zone_lo": round(zone_lo, 6),
            "pp_short_zone_hi": round(zone_hi, 6),
            "pp_short_bodies": bodies,
            "pp_short_swing_idx": pivot_idx,
        }

    bodies = _bodies_beyond(
        opens,
        closes,
        start_idx=pivot_idx,
        direction="above",
        level=zone_hi,
    )
    return {
        "pp_short_true": False,
        "pp_short_early": False,
        "pp_long_true": bodies >= _TRUE_BODIES_MIN,
        "pp_long_early": bodies == _EARLY_BODIES,
        "pp_long_zone_lo": round(zone_lo, 6),
        "pp_long_zone_hi": round(zone_hi, 6),
        "pp_long_bodies": bodies,
        "pp_long_swing_idx": pivot_idx,
    }


def detect_pp(work: pl.DataFrame, *, closed: bool = False) -> dict[str, Any]:
    """Detect PP short/long breaks on a single TF frame (1h or 15m)."""
    base: dict[str, Any] = {
        "pp_short_true": False,
        "pp_short_early": False,
        "pp_long_true": False,
        "pp_long_early": False,
    }
    if work is None or work.is_empty():
        return base
    if not {"open", "high", "low", "close"}.issubset(set(work.columns)):
        return base

    sh_mask, sl_mask = _swing_points(work, n=_SWING_N, include_unconfirmed_tail=False)
    short_pp = _pp_side(work, sh_mask, side="high", closed=closed)
    long_pp = _pp_side(work, sl_mask, side="low", closed=closed)
    out = {**base, **short_pp, **long_pp}
    out["pp_short_true"] = short_pp.get("pp_short_true", False)
    out["pp_short_early"] = short_pp.get("pp_short_early", False)
    out["pp_long_true"] = long_pp.get("pp_long_true", False)
    out["pp_long_early"] = long_pp.get("pp_long_early", False)
    if not closed:
        out["pp_short_true"] = False
        out["pp_long_true"] = False
    return out


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
    "detect_pp",
    "rsi_trendline_break",
    "structure_snapshot",
    "with_spec_columns",
]

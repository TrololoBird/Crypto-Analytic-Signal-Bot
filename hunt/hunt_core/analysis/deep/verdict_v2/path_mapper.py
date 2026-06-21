"""Pattern → ExpectedPath mapping."""
from __future__ import annotations

from typing import Any

from hunt_core.analysis.deep.verdict_v2._helpers import atr_from_row, clamp01, pct_move, safe_float
from hunt_core.analysis.deep.verdict_v2.types import (
    ExpectedPath,
    HorizonTopology,
    PathType,
    PatternConfidence,
)
from hunt_core.maps.forecast import _collect_downward_targets, _collect_upward_targets

_PATTERN_PATH: dict[str, PathType] = {
    "bull_pullback": "pullback_down",
    "bear_rally": "pullback_up",
    "trend_continuation": "continuation_up",
    "trend_acceleration": "continuation_up",
    "distribution": "pullback_down",
    "accumulation": "pullback_up",
    "long_squeeze": "squeeze_down",
    "short_squeeze": "squeeze_up",
    "range_bound": "range",
    "mean_reversion": "range",
    "liquidity_sweep": "breakout_down",
    "stop_hunt": "breakout_up",
}


def _path_direction(path: PathType) -> str:
    if path.endswith("_up") or path in {"continuation_up", "pullback_up", "breakout_up", "squeeze_up"}:
        return "long"
    if path.endswith("_down") or path in {"continuation_down", "pullback_down", "breakout_down", "squeeze_down"}:
        return "short"
    return "neutral"


def _move_bounds(row: dict[str, Any], direction: str) -> tuple[float, float]:
    price = safe_float(row.get("price"))
    atr = atr_from_row(row)
    atr_pct = (atr / price * 100) if price > 0 and atr > 0 else 2.0
    if direction == "long":
        targets, _ = _collect_upward_targets(row, price) if price > 0 else ([], [])
        if targets:
            mv = abs(pct_move(price, min(targets, key=lambda t: t - price)))
            return (max(0.5, mv * 0.4), mv)
    elif direction == "short":
        targets, _ = _collect_downward_targets(row, price) if price > 0 else ([], [])
        if targets:
            mv = abs(pct_move(price, max(targets, key=lambda t: price - t)))
            return (max(0.5, mv * 0.4), mv)
    return (atr_pct * 0.8, atr_pct * 2.5)


def _time_bounds(path: PathType) -> tuple[float, float]:
    if path == "range":
        return (12.0, 48.0)
    if "squeeze" in path:
        return (4.0, 18.0)
    if "pullback" in path:
        return (6.0, 36.0)
    return (8.0, 72.0)


def map_to_expected_path(
    row: dict[str, Any],
    patterns: PatternConfidence,
    topo: HorizonTopology,
) -> ExpectedPath:
    pid = patterns.primary.id
    path_type = _PATTERN_PATH.get(pid, "range")
    if topo.kind == "aligned_trend" and topo.a_dominant == "long":
        path_type = "continuation_up"
    elif topo.kind == "aligned_trend" and topo.a_dominant == "short":
        path_type = "continuation_down"
    direction = _path_direction(path_type)
    move_lo, move_hi = _move_bounds(row, direction)
    time_lo, time_hi = _time_bounds(path_type)
    price = safe_float(row.get("price"))
    atr = atr_from_row(row)
    if direction == "long":
        invalidation = price - atr * 1.5 if price > 0 else 0.0
    elif direction == "short":
        invalidation = price + atr * 1.5 if price > 0 else 0.0
    else:
        invalidation = price
    rank = clamp01(patterns.primary.raw_score + topo.coherence * 0.2 - (0.1 if patterns.ambiguous else 0))
    narrative = f"{path_type.replace('_', ' ')} via {pid}"
    if patterns.ambiguous:
        narrative += " (ambiguous pattern spread)"
    return ExpectedPath(
        type=path_type,
        direction=direction,  # type: ignore[arg-type]
        expected_move_pct=(round(move_lo, 2), round(move_hi, 2)),
        expected_time_h=(time_lo, time_hi),
        invalidation=round(invalidation, 6),
        probability_rank=round(rank, 3),
        narrative=narrative,
        supporting_patterns=[pid, *[a.id for a in patterns.alternatives]],
        topology=topo.kind,
    )

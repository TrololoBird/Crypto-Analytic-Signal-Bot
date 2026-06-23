"""Pattern → ExpectedPath mapping."""
from __future__ import annotations

from typing import Any

from hunt_core.deep.verdict_v2._helpers import atr_from_row, clamp01, pct_move, safe_float
from hunt_core.deep.verdict_v2.types import (
    ExpectedPath,
    HorizonTopology,
    PathType,
    PatternConfidence,
    TradePlan,
)
from hunt_core.shared.primitives.targets import (
    collect_downward_targets as _collect_downward_targets,
    collect_upward_targets as _collect_upward_targets,
)

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
    """Expected move = envelope of nearest→farthest TP ladder (not far liq magnet alone)."""
    price = safe_float(row.get("price"))
    atr = atr_from_row(row)
    atr_pct = (atr / price * 100) if price > 0 and atr > 0 else 2.0
    if direction == "long" and price > 0:
        targets, _ = _collect_upward_targets(row, price)
        if len(targets) >= 1:
            nearest = min(targets[:3])
            farthest = max(targets[:3])
            lo = abs(pct_move(price, nearest))
            hi = abs(pct_move(price, farthest))
            return (round(max(0.3, lo * 0.95), 2), round(max(lo, hi), 2))
    elif direction == "short" and price > 0:
        targets, _ = _collect_downward_targets(row, price)
        if len(targets) >= 1:
            nearest = max(targets[:3])
            farthest = min(targets[:3])
            lo = abs(pct_move(price, nearest))
            hi = abs(pct_move(price, farthest))
            return (round(max(0.3, lo * 0.95), 2), round(max(lo, hi), 2))
    return (round(atr_pct * 0.8, 2), round(atr_pct * 2.5, 2))


def _time_bounds(path: PathType) -> tuple[float, float]:
    if path == "range":
        return (12.0, 48.0)
    if "squeeze" in path:
        return (4.0, 18.0)
    if "pullback" in path:
        return (6.0, 36.0)
    return (8.0, 72.0)


_CONTINUATION_PATTERNS = frozenset({"trend_continuation", "trend_acceleration"})


def adjust_expected_move_from_plan(path: ExpectedPath, plan: TradePlan | None) -> ExpectedPath:
    """Derive expected-move lower bound from nearest TP (R3)."""
    if plan is None or path.direction == "neutral":
        return path
    from hunt_core.deep.verdict_v2.types import ExpectedPath as EP

    entry = plan.entry_reference or (plan.entry_zone[0] + plan.entry_zone[1]) / 2
    tp1 = plan.take_profit_1
    if entry <= 0 or tp1 <= 0:
        return path
    nearest_pct = abs(pct_move(entry, tp1))
    lo, hi = path.expected_move_pct
    lo = min(lo, nearest_pct * 0.95)
    hi = max(hi, nearest_pct)
    if hi < lo:
        hi = lo * 1.5
    return EP(
        type=path.type,
        direction=path.direction,
        expected_move_pct=(round(lo, 2), round(hi, 2)),
        expected_time_h=path.expected_time_h,
        invalidation=path.invalidation,
        probability_rank=path.probability_rank,
        narrative=path.narrative,
        supporting_patterns=list(path.supporting_patterns),
        topology=path.topology,
    )


def map_to_expected_path(
    row: dict[str, Any],
    patterns: PatternConfidence,
    topo: HorizonTopology,
) -> ExpectedPath:
    pid = patterns.primary.id
    path_type = _PATTERN_PATH.get(pid, "range")
    direction = _path_direction(path_type)
    if topo.kind == "aligned_trend" and direction != "neutral":
        if topo.a_dominant == "long" and pid in _CONTINUATION_PATTERNS and direction == "long":
            path_type = "continuation_up"
        elif topo.a_dominant == "short" and pid in _CONTINUATION_PATTERNS and direction == "short":
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

"""Shared helpers for verdict engines."""
from __future__ import annotations

import math
from typing import Any

from hunt_core.shared.facts.trend import trend_from_snapshot


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def conviction(long: float, short: float) -> float:
    return abs(long - short)


def dominant_side(
    long: float,
    short: float,
    margin: float = 0.08,
    *,
    weak_margin: float = 0.04,
) -> str:
    if long >= short + margin:
        return "long"
    if short >= long + margin:
        return "short"
    if long >= short + weak_margin:
        return "weak_long"
    if short >= long + weak_margin:
        return "weak_short"
    return "neutral"


def direction_bias(dominant: str) -> str:
    """Collapse weak variants to trade direction or neutral."""
    if dominant in {"long", "weak_long"}:
        return "long"
    if dominant in {"short", "weak_short"}:
        return "short"
    return "neutral"


def trend_scores_from_snap(snap: dict[str, Any]) -> tuple[float, float]:
    if not snap or snap.get("status") == "empty":
        return 0.5, 0.5
    trend = trend_from_snapshot(snap, require_adx=False)
    adx = safe_float(snap.get("adx14"))
    strength = clamp01(adx / 45.0) if adx > 0 else 0.35
    if trend == "bull":
        return clamp01(0.55 + strength * 0.4), clamp01(0.45 - strength * 0.3)
    if trend == "bear":
        return clamp01(0.45 - strength * 0.3), clamp01(0.55 + strength * 0.4)
    return 0.5, 0.5


def information_value_from_z(z: float | None) -> float:
    if z is None or not math.isfinite(z):
        return 0.4
    if abs(z) < 0.25:
        return 0.75
    return clamp01(0.5 + abs(z) / 4.0)


def coverage_ratio(present: int, expected: int) -> float:
    if expected <= 0:
        return 0.0
    return clamp01(present / expected)


def atr_from_row(row: dict[str, Any], tf_key: str = "4h") -> float:
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    snap = tf.get(tf_key) or tf.get("1h") or {}
    price = safe_float(row.get("price"))
    atr = safe_float(snap.get("atr14"))
    if atr > 0:
        return atr
    atr_pct = safe_float(snap.get("atr_pct"), 1.0)
    return price * atr_pct / 100.0 if price > 0 else 0.0


def pct_move(from_px: float, to_px: float) -> float:
    if from_px <= 0:
        return 0.0
    return (to_px - from_px) / from_px * 100.0


def rr_ratio(entry: float, target: float, stop: float, direction: str) -> float:
    if entry <= 0 or stop <= 0 or target <= 0:
        return 0.0
    if direction == "long":
        risk = entry - stop
        reward = target - entry
    else:
        risk = stop - entry
        reward = entry - target
    if risk <= 0:
        return 0.0
    return reward / risk

"""Shared helpers for the Expansion Engine — pure row/dict accessors, no side effects.

The engine reads an already-assembled tick ``row`` (the same dict Verdict V2 reads)
and never recomputes ATR/OI/structure from frames. All access goes through these
defensive accessors so a thin/partial row degrades to abstaining blocks rather than
raising.
"""
from __future__ import annotations

import math
from typing import Any

UP = "up"
DOWN = "down"
NEUTRAL = "neutral"


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


def opt_float(val: Any) -> float | None:
    """Like safe_float but preserves "missing" as None (for coverage accounting)."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def as_dict(val: Any) -> dict[str, Any]:
    return val if isinstance(val, dict) else {}


def market_of(row: dict[str, Any]) -> dict[str, Any]:
    return as_dict(row.get("market"))


def maps_of(row: dict[str, Any]) -> dict[str, Any]:
    return as_dict(row.get("maps"))


def structure_of(row: dict[str, Any]) -> dict[str, Any]:
    return as_dict(row.get("structure"))


def regime_of(row: dict[str, Any]) -> dict[str, Any]:
    return as_dict(row.get("regime"))


def timeframes_of(row: dict[str, Any]) -> dict[str, Any]:
    tf = row.get("timeframes")
    if isinstance(tf, dict) and tf:
        return tf
    return as_dict(row.get("tf"))


def tf_snap(row: dict[str, Any], key: str) -> dict[str, Any]:
    """Closed-bar TF snapshot, preferring the ``*_closed`` variant when present."""
    tf = timeframes_of(row)
    snap = tf.get(f"{key}_closed") or tf.get(key)
    return as_dict(snap)


def smooth_up(x: float, *, lo: float, hi: float) -> float:
    """Linear ramp 0→1 between ``lo`` and ``hi`` (clamped)."""
    if hi <= lo:
        return 1.0 if x >= hi else 0.0
    return clamp01((x - lo) / (hi - lo))


def smooth_down(x: float, *, lo: float, hi: float) -> float:
    """Linear ramp 1→0 between ``lo`` and ``hi`` (clamped)."""
    return 1.0 - smooth_up(x, lo=lo, hi=hi)


def pct_distance(price: float, level: float) -> float:
    if price <= 0:
        return 0.0
    return abs(level - price) / price * 100.0

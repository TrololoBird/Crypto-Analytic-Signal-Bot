"""Prokol (false break + reclaim) — Phase 4B structural primary."""
from __future__ import annotations

from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not (v == v):  # NaN
        return default
    return v


def _candle_fields(block: dict[str, Any]) -> tuple[float, float, float, float]:
    candle = block.get("candle") if isinstance(block.get("candle"), dict) else {}
    o = _safe_float(candle.get("open") or block.get("open"))
    h = _safe_float(candle.get("high") or block.get("high"))
    l = _safe_float(candle.get("low") or block.get("low"))
    c = _safe_float(candle.get("close") or block.get("close"))
    return o, h, l, c


def _wick_body_ratio(open_: float, high: float, low: float, close: float) -> float:
    """Body share of full range — low ratio = wick-dominated trap bar."""
    rng = high - low
    if rng <= 0:
        return 1.0
    body = abs(close - open_)
    return body / rng


def detect_prokol(
    *,
    level: float,
    break_direction: str,
    tf: dict[str, Any] | None = None,
    break_pct: float = 0.005,
    return_bars: int = 2,
) -> dict[str, Any]:
    """Prokol/trap: broke level >0.5% then reclaimed within 1–2 closed bars.

    ``break_direction`` is the trapped side: ``long`` = false upside break,
    ``short`` = false downside break.
    """
    out: dict[str, Any] = {
        "prokol": False,
        "trap_direction": break_direction,
        "tf_trap": False,
        "break_pct": break_pct,
    }
    if level <= 0 or not tf:
        return out

    d = break_direction.lower().strip()
    blocks: list[dict[str, Any]] = []
    for key in ("5m_closed", "15m_closed", "1h_closed"):
        block = tf.get(key)
        if isinstance(block, dict) and block.get("closed_bar"):
            blocks.append(block)
    if len(blocks) < 2:
        return out

    recent = blocks[-min(len(blocks), return_bars + 1) :]
    broke_idx = -1
    for i, block in enumerate(recent):
        _o, hi, lo, close = _candle_fields(block)
        if d == "long" and hi > level * (1.0 + break_pct):
            broke_idx = i
            break
        if d == "short" and lo < level * (1.0 - break_pct):
            broke_idx = i
            break
    if broke_idx < 0:
        return out

    reclaimed = False
    for block in recent[broke_idx + 1 : broke_idx + 1 + return_bars]:
        _o, _hi, _lo, close = _candle_fields(block)
        if d == "long" and close <= level:
            reclaimed = True
            break
        if d == "short" and close >= level:
            reclaimed = True
            break
    if not reclaimed:
        return out

    out["prokol"] = True
    r1h = tf.get("1h_closed") or tf.get("1h") or {}
    if isinstance(r1h, dict):
        o, hi, lo, c = _candle_fields(r1h)
        if hi > lo and _wick_body_ratio(o, hi, lo, c) < 0.30:
            out["tf_trap"] = True
    return out


__all__ = ["detect_prokol"]

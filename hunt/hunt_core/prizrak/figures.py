"""Фигуры — v1, deliberately simplified (per plan): narrowing-range proxy for
вымпел/клин, using the squeeze signal already computed in ``confluence.py``. Full
geometric recognition of флаг/ГиП/двойное дно-вершина is explicitly deferred to v2 —
this only tags an existing candidate, it does not create a new signal type.

Course rule reused directly: "чем сильнее сузились эти линии — тем быстрее будет выход
из структуры" (Bollinger squeeze narrowing = imminent breakout, already the same
signal ``confluence.compute_confluence`` flags as ``bb_squeeze``).
"""
from __future__ import annotations

from typing import Any

import numpy as np

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.confluence import _bb_width_pctile, _closes


def tag_squeeze_pattern(summary: dict[str, Any], *, ohlcv: list[list[float]], cfg: PrizrakConfig | None = None) -> dict[str, Any]:
    """Add a ``pattern`` field to a candidate summary if a narrowing-range (вымпел/
    клин proxy) is present. Never creates a new candidate, never gates an existing one.
    """
    cfg = cfg or PrizrakConfig.load()
    close = _closes(ohlcv)
    pctile = _bb_width_pctile(close)
    if pctile is not None and pctile <= cfg.squeeze_bb_pctile_max:
        direction = summary.get("action")
        summary["pattern"] = "вымпел_или_клин (squeeze proxy, v1)"
        summary["pattern_bb_pctile"] = round(pctile, 3)
        summary["geometry_confidence"] = min(1.0, summary.get("geometry_confidence", 0.5) + 0.05)
    return summary


__all__ = ["tag_squeeze_pattern"]

"""Market context from HTF (1w/1d)."""
from __future__ import annotations

from typing import Any

from hunt_core.deep.verdict_v2._helpers import safe_float, trend_scores_from_snap


def classify_market_context(row: dict[str, Any]) -> str:
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    w = tf.get("1w") or {}
    d = tf.get("1d") or {}
    if not w and not d:
        return "range"

    w_lg, w_sh = trend_scores_from_snap(w)
    d_lg, d_sh = trend_scores_from_snap(d)
    adx_d = safe_float(d.get("adx14"))
    trend_age = safe_float(d.get("trend_age"))

    if w_lg > 0.62 and d_lg > 0.58:
        if adx_d > 35 and trend_age > 20:
            return "bull_exhaustion"
        if d_sh > d_lg:
            return "bull_distribution"
        return "bull_trend"
    if w_sh > 0.62 and d_sh > 0.58:
        if d_lg > d_sh:
            return "bear_accumulation"
        return "bear_trend"
    return "range"

"""Composite regime classifier — ADX + volatility + squeeze/choppiness.

Research reports 24/25: single ADX is noisy on meme perps; ensemble votes
reduce false MTF vetoes vs ADX-only classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Meme perp thresholds (batch 1 + report 24 ensemble guidance)
ADX_TREND = 30.0
ADX_RANGE = 15.0
ATR_PCT_HIGH = 5.0
ATR_PCT_LOW = 2.0
BB_SQUEEZE_PCTILE = 0.25


@dataclass(frozen=True, slots=True)
class EnsembleRegime:
    """Composite label from three independent votes."""

    label: str  # trend_up | trend_down | range | squeeze | volatile_chop
    adx_vote: str  # trending | ranging | neutral
    vol_vote: str  # high | normal | low
    chop_vote: str  # squeeze | choppy | clean
    votes_agree: int  # 0–3


def _frame(tf: dict[str, Any], key: str) -> dict[str, Any]:
    row = tf.get(key)
    return row if isinstance(row, dict) else {}


def classify(tf: dict[str, Any], *, trend_1h: str = "neutral") -> EnsembleRegime:
    """Classify structure from closed 1h frame (fallback: live 1h)."""
    r1h = _frame(tf, "1h_closed") or _frame(tf, "1h")
    adx = float(r1h.get("adx14") or 0.0)
    atr_pct = float(r1h.get("atr_pct") or 0.0)
    squeeze_on = bool(r1h.get("squeeze_on"))
    bb_pctile = r1h.get("bb_width_pctile")
    bb_low = bb_pctile is not None and float(bb_pctile) <= BB_SQUEEZE_PCTILE

    if adx >= ADX_TREND:
        adx_vote = "trending"
    elif 0 < adx < ADX_RANGE:
        adx_vote = "ranging"
    else:
        adx_vote = "neutral"

    if atr_pct >= ATR_PCT_HIGH:
        vol_vote = "high"
    elif 0 < atr_pct < ATR_PCT_LOW:
        vol_vote = "low"
    else:
        vol_vote = "normal"

    if squeeze_on or bb_low:
        chop_vote = "squeeze"
    elif adx_vote == "ranging" and vol_vote == "high":
        chop_vote = "choppy"
    else:
        chop_vote = "clean"

    votes = {adx_vote, vol_vote, chop_vote}
    agree = 3 - len(votes) + 1 if len(votes) < 3 else 1

    if chop_vote == "squeeze":
        label = "squeeze"
    elif chop_vote == "choppy" and adx_vote != "trending":
        label = "volatile_chop"
    elif adx_vote == "trending":
        label = "trend_up" if trend_1h == "bull" else ("trend_down" if trend_1h == "bear" else "range")
    else:
        label = "range"

    return EnsembleRegime(
        label=label,
        adx_vote=adx_vote,
        vol_vote=vol_vote,
        chop_vote=chop_vote,
        votes_agree=agree,
    )

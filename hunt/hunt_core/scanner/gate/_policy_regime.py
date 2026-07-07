"""Ensemble regime classification and funding tiers (Phase 9 split)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hunt_core.toolkit.adx_thresholds import ADX_RANGE_MAX, ADX_TREND_MIN

ADX_TREND = ADX_TREND_MIN
ADX_RANGE = ADX_RANGE_MAX
ATR_PCT_HIGH = 5.0
ATR_PCT_LOW = 2.0
BB_SQUEEZE_PCTILE = 0.25


@dataclass(frozen=True, slots=True)
class EnsembleRegime:
    """Composite label from three independent votes."""

    label: str
    adx_vote: str
    vol_vote: str
    chop_vote: str
    votes_agree: int


def frame(tf: dict[str, Any], key: str) -> dict[str, Any]:
    row = tf.get(key)
    return row if isinstance(row, dict) else {}


_frame = frame  # backward compat for mtf module


def classify(tf: dict[str, Any], *, trend_1h: str = "neutral") -> EnsembleRegime:
    """Classify structure from closed 1h frame (fallback: live 1h)."""
    r1h = frame(tf, "1h_closed") or frame(tf, "1h")
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


FUNDING_SHORT_CONFIRM_MIN = 0.001
FUNDING_SQUEEZE_WARN = -0.0015
FUNDING_SQUEEZE_BLOCK = -0.002
FUNDING_SQUEEZE_MAX = FUNDING_SQUEEZE_BLOCK
BASIS_AP_OVERHEAT_BPS = 120.0
BASIS_AP_UNDERHEAT_BPS = -120.0


def resolve_market_funding_rate(mkt: dict[str, Any] | None) -> float | None:
    """Normalize funding to decimal rate per interval (Binance funding_rate scale)."""
    market = mkt if isinstance(mkt, dict) else {}
    funding = market.get("funding_live")
    if funding is None:
        funding = market.get("funding_rate")
    if funding is None and market.get("funding_pct") is not None:
        try:
            funding = float(market["funding_pct"]) / 100.0
        except (TypeError, ValueError):
            funding = None
    if funding is None:
        return None
    try:
        return float(funding)
    except (TypeError, ValueError):
        return None


def funding_short_risk_tier(fr: float | None) -> str:
    """ok | caution | block — tiered crowded-short gate."""
    if fr is None:
        return "ok"
    if fr <= FUNDING_SQUEEZE_BLOCK:
        return "block"
    if fr <= FUNDING_SQUEEZE_WARN:
        return "caution"
    return "ok"

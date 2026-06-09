"""Lightweight pump/dump hunt discovery — radar + hunt_scanner funnel (not delivery path)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

WatchBias = Literal["short", "long", "both"]

HUNT_MIN_QUOTE_VOLUME_USD = 10_000_000.0
HUNT_PUMP_EXTREME_PCT = 15.0
HUNT_RANGE_HOT_PCT = 8.0
HUNT_POS_NEAR_HIGH = 0.85
HUNT_POS_NEAR_LOW = 0.25
HUNT_SCORE_WATCH_THRESHOLD = 45.0
HUNT_SCORE_PRIORITY_THRESHOLD = 60.0


@dataclass(frozen=True, slots=True)
class HuntCandidate:
    symbol: str
    hunt_score: float
    watch_bias: WatchBias
    flags: tuple[str, ...]
    reasons: tuple[str, ...]
    last_price: float
    change_24h_pct: float
    quote_volume: float
    range_pct_24h: float | None
    pos_in_range: float | None


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def _range_stats(
    last_price: float,
    *,
    high_24h: float | None,
    low_24h: float | None,
) -> tuple[float | None, float | None]:
    if high_24h is None or low_24h is None or high_24h <= low_24h or last_price <= 0.0:
        return None, None
    range_pct = (high_24h / low_24h - 1.0) * 100.0
    pos = (last_price - low_24h) / (high_24h - low_24h)
    return round(range_pct, 2), round(max(0.0, min(1.0, pos)), 3)


def suggested_watch_bias(
    *,
    change_24h_pct: float,
    pos_in_range: float | None,
) -> WatchBias:
    if pos_in_range is not None:
        if pos_in_range >= HUNT_POS_NEAR_HIGH:
            return "short"
        if pos_in_range <= HUNT_POS_NEAR_LOW and change_24h_pct <= -8.0:
            return "long"
    if change_24h_pct >= HUNT_PUMP_EXTREME_PCT:
        return "short"
    if change_24h_pct <= -HUNT_PUMP_EXTREME_PCT:
        return "long"
    if abs(change_24h_pct) >= HUNT_RANGE_HOT_PCT:
        return "both"
    return "both"


def score_hunt_row(row: dict[str, Any]) -> HuntCandidate | None:
    """Score one normalized 24h ticker row for hunt watchlist candidacy."""
    symbol = str(row.get("symbol") or "").strip().upper()
    last_price = _safe_float(row.get("last_price"))
    quote_volume = _safe_float(row.get("quote_volume"), 0.0) or 0.0
    change_24h = _safe_float(row.get("price_change_percent") or row.get("price_change_pct"), 0.0) or 0.0
    if not symbol or last_price is None or last_price <= 0.0:
        return None
    if quote_volume < HUNT_MIN_QUOTE_VOLUME_USD:
        return None

    high_24h = _safe_float(row.get("high_price") or row.get("high_24h"))
    low_24h = _safe_float(row.get("low_price") or row.get("low_24h"))
    range_pct, pos = _range_stats(last_price, high_24h=high_24h, low_24h=low_24h)

    flags: list[str] = []
    reasons: list[str] = []
    score = 0.0
    move = abs(change_24h)

    if move >= HUNT_PUMP_EXTREME_PCT:
        score += 30.0
        flags.append("pump_extreme")
        reasons.append(f"change_24h={change_24h:.1f}%")
    elif move >= HUNT_RANGE_HOT_PCT:
        score += 18.0
        flags.append("range_hot")
        reasons.append(f"change_24h={change_24h:.1f}%")

    if range_pct is not None and range_pct >= 25.0:
        score += 20.0
        flags.append("range_expansion")
        reasons.append(f"range_24h={range_pct:.1f}%")

    if pos is not None:
        if pos >= HUNT_POS_NEAR_HIGH:
            score += 15.0
            flags.append("pos_near_high")
            reasons.append(f"pos_in_range={pos:.2f}")
        elif pos <= HUNT_POS_NEAR_LOW:
            score += 12.0
            flags.append("pos_near_low")
            reasons.append(f"pos_in_range={pos:.2f}")

    vol_score = min(math.log10(max(quote_volume, 1.0)) - 7.0, 2.0) / 2.0
    score += max(0.0, vol_score) * 10.0

    if move >= 25.0 and quote_volume >= 50_000_000:
        score += 8.0
        flags.append("liquid_mover")

    score = round(min(score, 100.0), 1)
    if score < 25.0:
        return None

    watch_bias = suggested_watch_bias(change_24h_pct=change_24h, pos_in_range=pos)
    return HuntCandidate(
        symbol=symbol,
        hunt_score=score,
        watch_bias=watch_bias,
        flags=tuple(flags),
        reasons=tuple(reasons[:6]),
        last_price=float(last_price),
        change_24h_pct=round(change_24h, 2),
        quote_volume=quote_volume,
        range_pct_24h=range_pct,
        pos_in_range=pos,
    )


def rank_hunt_candidates(rows: list[dict[str, Any]], *, limit: int = 30) -> list[HuntCandidate]:
    scored: list[HuntCandidate] = []
    for row in rows:
        candidate = score_hunt_row(row)
        if candidate is not None:
            scored.append(candidate)
    scored.sort(
        key=lambda item: (item.hunt_score, abs(item.change_24h_pct), item.quote_volume),
        reverse=True,
    )
    return scored[: max(limit, 1)]

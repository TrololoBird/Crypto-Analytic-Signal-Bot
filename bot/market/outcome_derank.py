"""Shortlist deranking from recent stop-loss clusters (outcome feedback loop)."""

from __future__ import annotations

import math

DEFAULT_LOOKBACK_DAYS = 7
SL_CLUSTER_THRESHOLD = 2
PENALTY_PER_SL = 0.08
MAX_PENALTY = 0.28
DEFAULT_HALF_LIFE_DAYS = 3.0


def decay_weight(age_days: float, *, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """Exponential half-life decay for an SL event age in days."""
    if age_days <= 0.0:
        return 1.0
    half_life = max(0.25, float(half_life_days))
    return math.exp(-math.log(2) * age_days / half_life)


def penalties_from_sl_counts(
    sl_counts: dict[str, int | float],
    *,
    cluster_threshold: int = SL_CLUSTER_THRESHOLD,
    penalty_per_sl: float = PENALTY_PER_SL,
    max_penalty: float = MAX_PENALTY,
    sl_event_ages_days: dict[str, list[float]] | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> dict[str, float]:
    """Map symbol → score penalty from recent SL frequency (optionally time-decayed)."""
    penalties: dict[str, float] = {}
    threshold = max(1, int(cluster_threshold))
    step = max(0.01, float(penalty_per_sl))
    cap = max(step, float(max_penalty))
    ages_by_symbol = {
        str(symbol).upper(): [float(age) for age in ages]
        for symbol, ages in (sl_event_ages_days or {}).items()
    }
    symbols = {str(symbol).upper() for symbol in sl_counts} | set(ages_by_symbol)

    for symbol_key in symbols:
        ages = ages_by_symbol.get(symbol_key)
        if ages:
            effective = sum(
                decay_weight(float(age), half_life_days=half_life_days) for age in ages
            )
        else:
            effective = float(sl_counts.get(symbol_key, 0) or 0)
        if effective < threshold:
            continue
        excess = effective - threshold + 1.0
        penalties[symbol_key] = round(min(cap, excess * step), 4)
    return penalties

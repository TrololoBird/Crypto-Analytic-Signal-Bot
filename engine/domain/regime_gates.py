"""Shared regime / activation gate helpers (report-2/4/5)."""

from __future__ import annotations

import math

_TREND_REGIME_SETUPS = frozenset({"volume_climax_reversal", "cvd_divergence"})


def effective_market_regime(
    market_regime: str,
    *,
    bias_4h: str = "neutral",
    price: float | None = None,
    poc: float | None = None,
) -> str:
    """Neutral→bull inference for filter purposes (does not mutate stored regime)."""
    regime = str(market_regime or "").lower()
    if regime in {"neutral", "ranging", "choppy", ""}:
        if str(bias_4h or "").lower() == "uptrend":
            if poc is None or poc <= 0.0:
                return "bull"
            if price is not None and math.isfinite(price) and price > poc:
                return "bull"
    return regime or "neutral"


def activation_supertrend_blocked(
    direction: str,
    st15: float | None,
    st1h: float | None,
) -> bool:
    if st15 is None or st1h is None:
        return False
    if direction == "short" and st15 > 0.0 and st1h > 0.0:
        return True
    if direction == "long" and st15 < 0.0 and st1h < 0.0:
        return True
    return False


def trend_regime_blocks_reversal(
    setup_id: str,
    market_regime: str,
    bias_1h: str,
    direction: str,
) -> tuple[bool, str | None]:
    regime = str(market_regime or "").lower()
    bias = str(bias_1h or "").lower()
    if setup_id not in _TREND_REGIME_SETUPS or regime != "trending":
        return False, None
    if direction == "short" and bias == "uptrend":
        return True, "activation_trend_regime_short_blocked"
    if direction == "long" and bias == "downtrend":
        return True, "activation_trend_regime_long_blocked"
    return False, None


def is_counter_trend_reversal(
    direction: str,
    *,
    bias_4h: str = "neutral",
    bear_regime: bool = False,
    confirmation_profile: str = "",
) -> bool:
    profile = str(confirmation_profile or "").lower()
    if profile not in {"countertrend_exhaustion", "divergence_reversal"}:
        return False
    bias = str(bias_4h or "").lower()
    if bear_regime and direction == "long":
        return True
    if direction == "long" and bias == "downtrend":
        return True
    if direction == "short" and bias == "uptrend":
        return True
    return False

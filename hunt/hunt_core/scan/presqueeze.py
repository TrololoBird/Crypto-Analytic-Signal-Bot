"""Pre-squeeze volatility coil path (§4.3)."""
from __future__ import annotations

from typing import Any

from hunt_core.domain.config import (
    SQUEEZE_BB_PCTILE_MAX,
    SQUEEZE_COOLDOWN_MINUTES,
    SQUEEZE_DONCHIAN_MAX_PCT,
    SQUEEZE_MIN_VOL_24H_M,
)


def squeeze_watch(tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any] | None:
    """Charged state: 1h BB-width in bottom quintile + narrow Donchian channel."""
    r1h = tf.get("1h") or {}
    pctile = r1h.get("bb_width_pctile")
    don = r1h.get("donchian_width_pct")
    if pctile is None or don is None:
        return None
    charged = float(pctile) <= SQUEEZE_BB_PCTILE_MAX and float(don) <= SQUEEZE_DONCHIAN_MAX_PCT
    if not charged:
        return None
    return {
        "charged": True,
        "bb_width_pctile_1h": float(pctile),
        "donchian_width_pct_1h": float(don),
        "squeeze_on_1h": r1h.get("squeeze_on"),
        "oi_z": market.get("oi_z"),
        "gls_z": market.get("gls_z"),
        "funding_pct": market.get("funding_pct"),
    }


def evaluate_presqueeze(tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any] | None:
    return squeeze_watch(tf, market)


def format_squeeze_telegram(row: dict[str, Any]) -> str:
    from hunt_core.deliver.templates import format_squeeze_telegram as _fmt

    return _fmt(row)


__all__ = [
    "SQUEEZE_BB_PCTILE_MAX",
    "SQUEEZE_COOLDOWN_MINUTES",
    "SQUEEZE_DONCHIAN_MAX_PCT",
    "SQUEEZE_MIN_VOL_24H_M",
    "evaluate_presqueeze",
    "format_squeeze_telegram",
    "squeeze_watch",
]

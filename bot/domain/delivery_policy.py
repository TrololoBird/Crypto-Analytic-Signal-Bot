"""Delivery policy helpers — R-class WATCH-only and benchmark anchors (target spec)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bot.regime.market import BEAR_BIAS_VALUES, BEAR_MACRO_RISK_MODES, BEAR_MARKET_REGIMES

from .config import REQUIRED_PINNED_SYMBOLS

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .config import BotSettings

# Positioning / funding-OI setups: elevated positioning is signal, not "calm market".
POSITIONING_SETUP_IDS: frozenset[str] = frozenset(
    {
        "funding_reversal",
        "oi_divergence",
        "ls_ratio_extreme",
        "liquidation_heatmap",
    }
)

# Microstructure / sub-minute setups: no solo ACTION until redesign.
R_CLASS_SETUP_IDS: frozenset[str] = frozenset(
    {
        "price_velocity",
        "whale_walls",
        "spread_strategy",
        "depth_imbalance",
    }
)

BENCHMARK_ANCHOR_SYMBOLS: frozenset[str] = frozenset(REQUIRED_PINNED_SYMBOLS)

METAL_ANCHOR_SYMBOLS: frozenset[str] = frozenset({"XAUUSDT", "XAGUSDT", "PAXGUSDT"})


def _norm_bias(value: object | None) -> str:
    return str(value or "").strip().lower()


def resolve_bear_regime(
    *,
    market_ctx: Mapping[str, object] | None = None,
    prepared_btc_bias: str | None = None,
    signal_btc_bias: str | None = None,
) -> tuple[bool, str]:
    """True when BTC/global context is bearish (not alt regime_1h alone).

    Priority for ``bear_regime_source``: signal → prepared → market_ctx btc_bias
    → market_ctx regime → market_ctx macro_risk_mode.
    """
    signal_bias = _norm_bias(signal_btc_bias)
    if signal_bias in BEAR_BIAS_VALUES:
        return True, "signal_btc_bias"

    prepared_bias = _norm_bias(prepared_btc_bias)
    if prepared_bias in BEAR_BIAS_VALUES:
        return True, "prepared_btc_bias"

    if market_ctx:
        ctx_btc = _norm_bias(market_ctx.get("btc_bias"))
        if ctx_btc in BEAR_BIAS_VALUES:
            return True, "market_ctx_btc_bias"

        ctx_regime = _norm_bias(market_ctx.get("market_regime"))
        if ctx_regime in BEAR_MARKET_REGIMES:
            return True, "market_ctx_regime"

        macro = _norm_bias(market_ctx.get("macro_risk_mode"))
        if macro in BEAR_MACRO_RISK_MODES:
            return True, "market_ctx_macro_risk"

    return False, "none"


def is_r_class_setup(setup_id: str) -> bool:
    return str(setup_id or "").strip() in R_CLASS_SETUP_IDS


def is_positioning_setup(setup_id: str) -> bool:
    return str(setup_id or "").strip() in POSITIONING_SETUP_IDS


def is_benchmark_anchor(symbol: str) -> bool:
    """True for pinned benchmark majors (BENCHMARK_ANCHORS.md)."""
    key = str(symbol or "").strip().upper()
    return bool(key) and key in BENCHMARK_ANCHOR_SYMBOLS


def is_metal_anchor(symbol: str) -> bool:
    key = str(symbol or "").strip().upper()
    return bool(key) and key in METAL_ANCHOR_SYMBOLS


def effective_action_min_score(settings: BotSettings, symbol: str) -> float:
    """Higher ACTION bar on benchmark anchors (+delta vs alts)."""
    delivery = settings.delivery
    base = float(delivery.action_min_score)
    if not is_benchmark_anchor(symbol):
        return base
    delta = float(delivery.anchor_action_score_delta)
    if is_metal_anchor(symbol):
        delta += float(delivery.metal_action_score_delta)
    return min(1.0, base + max(0.0, delta))


def r_class_blocks_action(setup_id: str, settings: object) -> bool:
    """Return True when Telegram ACTION must not be sent for this setup."""
    if not is_r_class_setup(setup_id):
        return False
    delivery = getattr(settings, "delivery", None)
    if delivery is None:
        return True
    return bool(getattr(delivery, "r_class_watch_only", True))

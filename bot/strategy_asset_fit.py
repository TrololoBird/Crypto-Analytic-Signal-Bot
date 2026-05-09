"""Strategy-to-asset fit declarations and routing helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from math import isfinite
from typing import Literal

from .models import PreparedSymbol

VolatilityRegime = Literal["low", "medium", "high", "any"]

PRIORITY_ASSETS = frozenset({"BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT"})
MAJOR_ASSETS = frozenset({"BTCUSDT", "ETHUSDT"})
METAL_ASSETS = frozenset({"XAUUSDT", "XAGUSDT"})


@dataclass(frozen=True, slots=True)
class AssetFit:
    """Declarative strategy routing profile for symbol-level calibration."""

    applies_to: frozenset[str] = field(default_factory=lambda: frozenset({"all"}))
    excludes: frozenset[str] = field(default_factory=frozenset)
    min_liquidity_rank: int = 100
    requires_funding: bool = False
    requires_oi: bool = False
    preferred_timeframes: tuple[str, ...] = ("15m", "1h")
    volatility_regime: VolatilityRegime = "any"

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation for telemetry/UI metadata."""
        return {
            "applies_to": sorted(self.applies_to),
            "excludes": sorted(self.excludes),
            "min_liquidity_rank": self.min_liquidity_rank,
            "requires_funding": self.requires_funding,
            "requires_oi": self.requires_oi,
            "preferred_timeframes": list(self.preferred_timeframes),
            "volatility_regime": self.volatility_regime,
        }


def _fit(
    applies_to: tuple[str, ...] = ("all",),
    *,
    excludes: tuple[str, ...] = (),
    min_liquidity_rank: int = 100,
    requires_funding: bool = False,
    requires_oi: bool = False,
    preferred_timeframes: tuple[str, ...] = ("15m", "1h"),
    volatility_regime: VolatilityRegime = "any",
) -> AssetFit:
    return AssetFit(
        applies_to=frozenset(item.upper() for item in applies_to),
        excludes=frozenset(item.upper() for item in excludes),
        min_liquidity_rank=min_liquidity_rank,
        requires_funding=requires_funding,
        requires_oi=requires_oi,
        preferred_timeframes=preferred_timeframes,
        volatility_regime=volatility_regime,
    )


DEFAULT_ASSET_FIT = _fit()

ASSET_FIT_PROFILES: dict[str, AssetFit] = {
    "structure_pullback": _fit(("all",)),
    "structure_break_retest": _fit(("all",)),
    "wick_trap_reversal": _fit(("all",)),
    "squeeze_setup": _fit(("all",)),
    "ema_bounce": _fit(("all",)),
    "fvg_setup": _fit(("all",)),
    "order_block": _fit(("all",)),
    "liquidity_sweep": _fit(("all",), min_liquidity_rank=50),
    "bos_choch": _fit(("all",)),
    "hidden_divergence": _fit(("all",)),
    "funding_reversal": _fit(
        ("perp", "majors"),
        excludes=("XAUUSDT", "XAGUSDT"),
        requires_funding=True,
        requires_oi=True,
    ),
    "cvd_divergence": _fit(("perp",), min_liquidity_rank=80),
    "session_killzone": _fit(("all",)),
    "breaker_block": _fit(("all",)),
    "turtle_soup": _fit(("all",)),
    "vwap_trend": _fit(("majors", "high_volume"), min_liquidity_rank=20),
    "supertrend_follow": _fit(("all",)),
    "price_velocity": _fit(("volatile", "high_volume"), min_liquidity_rank=50),
    "volume_anomaly": _fit(("all",)),
    "volume_climax_reversal": _fit(("all",)),
    "keltner_breakout": _fit(("all",)),
    "whale_walls": _fit(("all",), min_liquidity_rank=50),
    "spread_strategy": _fit(("all",), min_liquidity_rank=50),
    "depth_imbalance": _fit(("all",), min_liquidity_rank=50),
    "absorption": _fit(("all",), min_liquidity_rank=50),
    "aggression_shift": _fit(("all",), min_liquidity_rank=50),
    "liquidation_heatmap": _fit(("perp",), min_liquidity_rank=80),
    "stop_hunt_detection": _fit(("all",)),
    "multi_tf_trend": _fit(("all",), preferred_timeframes=("1h", "4h")),
    "rsi_divergence_bottom": _fit(("all",)),
    "wyckoff_spring": _fit(("all",)),
    "bb_squeeze": _fit(("all",)),
    "atr_expansion": _fit(("all",)),
    "ls_ratio_extreme": _fit(("perp",), requires_oi=True),
    "oi_divergence": _fit(("perp",), requires_oi=True),
    "btc_correlation": _fit(("all",), excludes=(), preferred_timeframes=("1h", "4h")),
    "altcoin_season_index": _fit(
        ("all",), excludes=(), preferred_timeframes=("1h", "4h")
    ),
}


def symbol_asset_tags(symbol: str, base_asset: str | None = None) -> frozenset[str]:
    """Classify a USD-M symbol into routing tags used by asset-fit profiles."""
    normalized = str(symbol or "").strip().upper()
    base = str(base_asset or "").strip().upper()
    if not base and normalized.endswith("USDT"):
        base = normalized.removesuffix("USDT")
    tags = {normalized, base, "PERP"}
    if normalized in PRIORITY_ASSETS:
        tags.add("PRIORITY")
    if normalized in MAJOR_ASSETS:
        tags.add("MAJORS")
    if normalized in METAL_ASSETS:
        tags.add("METALS")
    if normalized not in MAJOR_ASSETS and normalized not in METAL_ASSETS:
        tags.add("ALTS")
        tags.add("VOLATILE_ALTS")
    return frozenset(tag for tag in tags if tag)


def market_asset_tags(
    symbol: str,
    market_context: Mapping[str, object],
) -> frozenset[str]:
    """Return static plus market-dependent tags for strategy routing.

    Static tags alone are not enough for profiles such as ``price_velocity``,
    which explicitly targets volatile/high-volume symbols. Those tags depend on
    the current shortlist context and must be derived before asset-fit scoring.
    """
    base_asset = str(market_context.get("base_asset") or "").strip().upper()
    tags = set(symbol_asset_tags(symbol, base_asset))

    rank_raw = market_context.get("liquidity_rank")
    rank = (
        int(rank_raw)
        if isinstance(rank_raw, int | float) and isfinite(float(rank_raw))
        else None
    )
    shortlist_limit_raw = market_context.get("shortlist_limit")
    shortlist_limit = (
        int(shortlist_limit_raw)
        if isinstance(shortlist_limit_raw, int | float)
        and isfinite(float(shortlist_limit_raw))
        and int(shortlist_limit_raw) > 0
        else None
    )
    high_volume_rank_cutoff = (
        max(1, int(math.ceil(shortlist_limit * 0.4)))
        if shortlist_limit is not None
        else 50
    )
    if rank is not None and rank <= high_volume_rank_cutoff:
        tags.add("HIGH_VOLUME")

    quote_volume_raw = market_context.get("quote_volume")
    if isinstance(quote_volume_raw, int | float) and isfinite(float(quote_volume_raw)):
        if float(quote_volume_raw) >= 30_000_000.0:
            tags.add("HIGH_VOLUME")

    move_raw = market_context.get("price_change_pct")
    move = abs(float(move_raw)) if isinstance(move_raw, int | float) else 0.0
    if move >= 2.0:
        tags.add("VOLATILE")
    if move >= 8.0:
        tags.add("HIGH_VOLATILITY")

    return frozenset(tags)


def market_context_from_prepared(prepared: PreparedSymbol) -> dict[str, object]:
    """Extract asset-fit market context from a prepared symbol."""
    universe = prepared.universe
    return {
        "symbol": prepared.symbol,
        "base_asset": universe.base_asset,
        "liquidity_rank": universe.liquidity_rank,
        "quote_volume": universe.quote_volume,
        "price_change_pct": universe.price_change_pct,
        "funding_rate": prepared.funding_rate,
        "oi_current": prepared.oi_current,
        "oi_change_pct": prepared.oi_change_pct,
        "depth_imbalance": prepared.depth_imbalance,
        "microprice_bias": prepared.microprice_bias,
    }


def asset_fit_for_strategy(strategy_id: str) -> AssetFit:
    """Return the declared asset-fit profile for a strategy id."""
    return ASSET_FIT_PROFILES.get(strategy_id, DEFAULT_ASSET_FIT)


def asset_fit_reject_reason(
    strategy_id: str,
    symbol: str,
    market_context: Mapping[str, object],
    *,
    settings: object | None = None,
) -> str | None:
    """Return a calibrated rejection reason when a strategy does not fit a symbol."""
    normalized_symbol = str(symbol or market_context.get("symbol") or "").strip().upper()
    profile = asset_fit_for_strategy(strategy_id)
    context = dict(market_context)
    universe = getattr(settings, "universe", None) if settings is not None else None
    if universe is not None and "shortlist_limit" not in context:
        context["shortlist_limit"] = getattr(universe, "shortlist_limit", None)
    tags = market_asset_tags(normalized_symbol, context)

    assets = getattr(settings, "assets", {}) if settings is not None else {}
    asset_config = assets.get(normalized_symbol) if isinstance(assets, dict) else None
    excluded = getattr(asset_config, "excluded_strategies", ()) if asset_config is not None else ()
    if strategy_id in set(str(item) for item in excluded):
        return "asset_fit.config_excluded"

    if normalized_symbol in profile.excludes or profile.excludes.intersection(tags):
        return "asset_fit.symbol_excluded"
    if "ALL" not in profile.applies_to and not profile.applies_to.intersection(tags):
        return "asset_fit.scope_mismatch"

    rank_raw = market_context.get("liquidity_rank")
    rank = int(rank_raw) if isinstance(rank_raw, int | float) and isfinite(float(rank_raw)) else None
    if rank is not None and rank > int(profile.min_liquidity_rank):
        return "asset_fit.liquidity_rank_too_low"

    funding_rate = market_context.get("funding_rate")
    if profile.requires_funding and funding_rate is None:
        return "asset_fit.funding_missing"

    oi_current = market_context.get("oi_current")
    oi_change_pct = market_context.get("oi_change_pct")
    if profile.requires_oi and oi_current is None and oi_change_pct is None:
        return "asset_fit.oi_missing"

    return None


def calculate_strategy_fit_score(
    symbol: str,
    strategy_id: str,
    market_context: Mapping[str, object],
    *,
    settings: object | None = None,
) -> float:
    """Score symbol/strategy fit from routing, liquidity, freshness, and volatility."""
    if asset_fit_reject_reason(strategy_id, symbol, market_context, settings=settings) is not None:
        return 0.0

    profile = asset_fit_for_strategy(strategy_id)
    rank_raw = market_context.get("liquidity_rank")
    rank = float(rank_raw) if isinstance(rank_raw, int | float) else float(profile.min_liquidity_rank)
    liquidity_score = max(0.0, min(1.0, 1.0 - (rank - 1.0) / max(float(profile.min_liquidity_rank), 1.0)))

    data_score = 1.0
    if profile.requires_funding and market_context.get("funding_rate") is None:
        data_score -= 0.5
    if profile.requires_oi and market_context.get("oi_current") is None and market_context.get("oi_change_pct") is None:
        data_score -= 0.5

    move_raw = market_context.get("price_change_pct")
    move = abs(float(move_raw)) if isinstance(move_raw, int | float) else 0.0
    if profile.volatility_regime == "low":
        volatility_score = 1.0 if move < 2.0 else 0.55
    elif profile.volatility_regime == "medium":
        volatility_score = 1.0 if 1.0 <= move <= 6.0 else 0.65
    elif profile.volatility_regime == "high":
        volatility_score = 1.0 if move >= 4.0 else 0.55
    else:
        volatility_score = 1.0

    return round(max(0.0, min(1.0, 0.45 * liquidity_score + 0.35 * data_score + 0.20 * volatility_score)), 6)

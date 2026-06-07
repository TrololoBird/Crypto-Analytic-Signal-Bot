from __future__ import annotations

import logging
import math
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from bot.coercion import row_float
from bot.runtime.errors import DEFENSIVE_EXC

from ..domain.config import _ALL_SETUP_IDS, BotSettings
from ..domain.schemas import SymbolMeta, UniverseSymbol
from ..market.fit import calculate_strategy_fit_score
from ..market.strategy_pools import asset_strategy_allowlist, fill_shortlist_from_pools

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

LOG = logging.getLogger("bot.universe")
DEFAULT_PRESCORE_BASIS_WARM_LIMIT = 25
STABLE_BASE_ASSETS = {"USDC", "BUSD", "FDUSD", "TUSD", "USDP", "USDS", "DAI"}
SUPPORTED_USDM_CONTRACT_TYPES = {"PERPETUAL", "TRADIFI_PERPETUAL"}
_ASCII_CONTRACT_RE = re.compile(r"^[A-Z0-9]{4,24}$")
_ASCII_ASSET_RE = re.compile(r"^[A-Z0-9]{2,16}$")
_RESERVED_PER_STRATEGY = 2
_PRICE_ACTION_COVERAGE_SETUP_IDS: tuple[str, ...] = (
    "structure_break_retest",
    "squeeze_setup",
    "bb_squeeze",
    "atr_expansion",
    "bos_choch",
    "order_block",
    "breaker_block",
    "price_velocity",
    "volume_anomaly",
    "wick_trap_reversal",
    "hidden_divergence",
    "rsi_divergence_bottom",
    "turtle_soup",
    "liquidity_sweep",
    "stop_hunt_detection",
    "wyckoff_spring",
    "volume_climax_reversal",
)
_DEEP_ANALYSIS_PRIORITY_SYMBOLS: tuple[str, ...] = (
    "XRPUSDT",
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XAUUSDT",
    "XAGUSDT",
    "PAXGUSDT",
)
_DEEP_ANALYSIS_PRIORITY_BASES: frozenset[str] = frozenset(
    {"BTC", "ETH", "SOL", "XRP", "XAU", "XAG", "PAXG", "PAX"}
)


def _configured_deep_analysis_symbols(settings: BotSettings) -> set[str]:
    assets = getattr(settings, "assets", {}) or {}
    symbols: set[str] = set()
    if isinstance(assets, dict):
        for symbol, config in assets.items():
            if bool(getattr(config, "deep_analysis", False)):
                symbols.add(str(symbol).strip().upper())
    return symbols


def _priority_symbols(settings: BotSettings) -> set[str]:
    configured = _configured_deep_analysis_symbols(settings)
    pinned = {
        str(symbol).strip().upper()
        for symbol in getattr(settings.universe, "pinned_symbols", ())
        if str(symbol).strip()
    }
    return configured | pinned


def _is_priority_asset(row: dict[str, Any], settings: BotSettings) -> bool:
    symbol = str(row.get("symbol") or "").strip().upper()
    base_asset = str(row.get("base_asset") or "").strip().upper()
    return symbol in _priority_symbols(settings) or base_asset in _DEEP_ANALYSIS_PRIORITY_BASES


def _bucket_for_price_change(price_change_pct: float) -> str:
    move = abs(float(price_change_pct))
    if move >= 8.0:
        return "reversal"
    if move >= 2.0:
        return "breakout"
    return "trend"


def _scaled_bucket_targets(
    total_slots: int,
    *,
    market_regime: str | None = None,
) -> dict[str, int]:
    base = {"trend": 12, "breakout": 10, "reversal": 8}
    regime = str(market_regime or "").strip().lower()
    if regime in {"bear", "decline", "risk_off"}:
        base = {"trend": 6, "breakout": 8, "reversal": 14}
    elif regime in {"bull", "expansion", "uptrend"}:
        base = {"trend": 14, "breakout": 12, "reversal": 6}
    if total_slots <= 0:
        return dict.fromkeys(base, 0)
    base_total = sum(base.values())
    scaled = {key: round(total_slots * weight / base_total) for key, weight in base.items()}
    assigned = sum(scaled.values())
    if assigned < total_slots:
        for key in ("trend", "breakout", "reversal"):
            if assigned >= total_slots:
                break
            scaled[key] += 1
            assigned += 1
    elif assigned > total_slots:
        for key in ("reversal", "breakout", "trend"):
            while assigned > total_slots and scaled[key] > 0:
                scaled[key] -= 1
                assigned -= 1
    return scaled


def _is_supported_contract_symbol(symbol: str, base_asset: str) -> bool:
    if not _ASCII_CONTRACT_RE.fullmatch(symbol):
        return False
    return _ASCII_ASSET_RE.fullmatch(base_asset)


def _bucket_priority(item: UniverseSymbol) -> tuple[float, float, str]:
    move = abs(item.price_change_pct)
    volume_score = math.log10(max(item.quote_volume, 1.0))
    if item.shortlist_bucket == "trend":
        move_fit = max(0.0, 1.0 - min(move, 2.0) / 2.0)
    elif item.shortlist_bucket == "breakout":
        move_fit = max(0.0, 1.0 - min(abs(move - 4.5) / 4.5, 1.0))
    else:
        move_fit = max(0.0, 1.0 - min(abs(move - 11.0) / 12.0, 1.0))
    quality = round(move_fit * 10.0 + volume_score, 6)
    return quality, item.quote_volume, item.symbol


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        numeric = float(value)
    except TypeError, ValueError:
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def _oi_change_percent(value: Any) -> float | None:
    """Normalize cached OI change to percentage points for shortlist routing.

    The REST cache stores 1h OI change as a fraction (`0.03` == 3%), while
    older telemetry/config thresholds used percentage points (`3.0` == 3%).
    Accept both forms at the universe boundary so strategy routing is stable.
    """
    numeric = _safe_float(value)
    if numeric is None:
        return None
    if abs(numeric) <= 1.0:
        return numeric * 100.0
    return numeric


def _crowding_score(row: dict[str, Any]) -> float:
    ratios = []
    for key in (
        "top_account_ls_ratio",
        "top_position_ls_ratio",
        "global_account_ls_ratio",
    ):
        ratio = _safe_float(row.get(key))
        if ratio is not None and ratio > 0.0:
            ratios.append(ratio)
    gap = _safe_float(row.get("top_vs_global_ls_gap"))
    if not ratios and gap is None:
        return 0.55

    penalties: list[float] = []
    for ratio in ratios:
        deviation = abs(ratio - 1.0)
        penalties.append(_clamp((deviation - 0.12) / 0.95))
    if gap is not None:
        penalties.append(_clamp((abs(gap) - 0.05) / 0.45))
    penalty = sum(penalties) / len(penalties) if penalties else 0.0
    return round(_clamp(1.0 - penalty), 6)


def _oi_participation_score(row: dict[str, Any]) -> float:
    oi_change = _oi_change_percent(row.get("oi_change_pct"))
    oi_current = _safe_float(row.get("oi_current"))
    quote_volume = float(row.get("quote_volume") or 0.0)
    last_price = float(row.get("last_price") or 0.0)

    change_score = 0.55
    if oi_change is not None:
        if oi_change >= 12.0:
            change_score = 1.0
        elif oi_change >= 5.0:
            change_score = 0.82
        elif oi_change >= 1.5:
            change_score = 0.65
        elif oi_change <= -8.0:
            change_score = 0.18
        elif oi_change <= -2.0:
            change_score = 0.35
        else:
            change_score = 0.5

    notional_score = 0.55
    if oi_current is not None and oi_current > 0.0 and quote_volume > 0.0 and last_price > 0.0:
        oi_notional_ratio = (oi_current * last_price) / quote_volume
        notional_score = _clamp(oi_notional_ratio / 1.6)
    return round(change_score * 0.65 + notional_score * 0.35, 6)


def _funding_basis_sanity_score(row: dict[str, Any], settings: BotSettings) -> float:
    funding_rate = _safe_float(row.get("funding_rate"))
    basis_pct = _safe_float(row.get("basis_pct"))
    priority_asset = _is_priority_asset(row, settings)
    if funding_rate is None and basis_pct is None:
        return 0.68 if priority_asset else 0.55

    funding_score = 0.7
    if funding_rate is not None:
        funding_abs = abs(funding_rate)
        if funding_abs <= 0.0004:
            funding_score = 0.9
        elif funding_abs <= 0.0008:
            funding_score = 0.72
        elif funding_abs <= 0.0012:
            funding_score = 0.45
        else:
            funding_score = 0.15

    basis_score = 0.7
    if basis_pct is not None:
        basis_abs = abs(basis_pct)
        if basis_abs <= 0.05:
            basis_score = 0.9
        elif basis_abs <= 0.12:
            basis_score = 0.72
        elif basis_abs <= 0.2:
            basis_score = 0.45
        else:
            basis_score = 0.15
    score = funding_score * 0.5 + basis_score * 0.5
    if priority_asset:
        score = max(score, 0.68)
    return round(score, 6)


def _wash_volume_score(row: dict[str, Any]) -> float:
    """Score average quote trade size; very small prints suggest wash volume."""
    quote_volume = float(row.get("quote_volume") or 0.0)
    trade_count = int(float(row.get("trade_count") or 0.0))
    if quote_volume <= 0.0 or trade_count <= 0:
        return 0.55
    avg_trade_quote = quote_volume / float(trade_count)
    # ~$100/trade is weak; ~$10k+ is strong organic flow.
    score = _clamp((math.log10(max(avg_trade_quote, 1.0)) - 2.0) / 2.5 + 0.35)
    return round(score, 6)


def _microstructure_opportunity_score(row: dict[str, Any]) -> float:
    """Score whether the row has actionable public microstructure context."""
    components: list[float] = []
    taker_ratio = _safe_float(row.get("taker_ratio"))
    if taker_ratio is not None and taker_ratio > 0.0:
        taker_move = abs(taker_ratio - 1.0)
        components.append(_clamp(0.45 + min(taker_move / 0.65, 1.0) * 0.45))
    liq_score = _safe_float(row.get("liquidation_score"))
    if liq_score is not None:
        components.append(_clamp(0.45 + abs(liq_score) * 0.45))
    premium_z = _safe_float(row.get("premium_zscore_5m"))
    if premium_z is not None:
        components.append(_clamp(0.45 + min(abs(premium_z) / 2.5, 1.0) * 0.40))
    premium_slope = _safe_float(row.get("premium_slope_5m"))
    if premium_slope is not None:
        components.append(_clamp(0.50 + min(abs(premium_slope) / 0.08, 1.0) * 0.25))
    if not components:
        return 0.55
    return round(sum(components) / len(components), 6)


def _strategy_fits_for_row(
    row: dict[str, Any],
    *,
    settings: BotSettings,
    liquidity_rank: int | None,
) -> tuple[str, ...]:
    fits: list[str] = []
    funding_rate = _safe_float(row.get("funding_rate"))
    basis_pct = _safe_float(row.get("basis_pct"))
    oi_change_pct = _oi_change_percent(row.get("oi_change_pct"))
    quote_volume = float(row.get("quote_volume") or 0.0)
    price_change_pct = abs(
        float(row.get("price_change_percent") or row.get("price_change_pct") or 0.0)
    )
    spread_bps = _safe_float(row.get("spread_bps"))
    crowding = _crowding_score(row)
    symbol = str(row.get("symbol") or "").strip().upper()
    pinned_set = {str(s).strip().upper() for s in settings.universe.pinned_symbols}

    volume_floor = max(float(settings.universe.min_quote_volume_usd), 1.0)
    volume_multiple = quote_volume / volume_floor
    spread_ok = spread_bps is None or spread_bps <= float(
        settings.universe.shortlist_spread_max_bps
    )
    liquid_enough = quote_volume >= max(volume_floor * 3.0, 30_000_000.0)
    top_liquidity = liquidity_rank is not None and liquidity_rank <= max(
        int(getattr(settings.universe, "shortlist_limit", 50)),
        30,
    )
    trending_move = price_change_pct <= 3.0
    breakout_move = 2.0 <= price_change_pct <= 10.0
    reversal_move = price_change_pct >= 5.0
    oi_rising = oi_change_pct is not None and oi_change_pct >= 1.0
    oi_extreme = oi_change_pct is not None and abs(oi_change_pct) >= 3.0
    crowd_extreme = crowding <= 0.45
    priority_asset = _is_priority_asset(row, settings)

    if spread_ok and volume_multiple >= 1.0 and trending_move:
        fits.extend(
            (
                "ema_bounce",
                "structure_pullback",
                "vwap_trend",
                "supertrend_follow",
                "multi_tf_trend",
                "fvg_setup",
                "cvd_divergence",
                "indicator_divergence",
                "stop_hunt_detection",
                "wyckoff_spring",
                "btc_correlation",
                "altcoin_season_index",
            )
        )
    if spread_ok and volume_multiple >= 1.5 and (breakout_move or oi_rising):
        fits.extend(
            (
                "structure_break_retest",
                "squeeze_setup",
                "bb_squeeze",
                "atr_expansion",
                "bos_choch",
                "fvg_setup",
                "order_block",
                "breaker_block",
                "session_killzone",
                "price_velocity",
                "volume_anomaly",
                "keltner_breakout",
                "spread_strategy",
                "depth_imbalance",
                "whale_walls",
                "aggression_shift",
            )
        )
    if spread_ok and liquid_enough and (reversal_move or crowd_extreme or oi_extreme):
        fits.extend(
            (
                "wick_trap_reversal",
                "hidden_divergence",
                "rsi_divergence_bottom",
                "turtle_soup",
                "liquidity_sweep",
                "stop_hunt_detection",
                "wyckoff_spring",
                "liquidation_heatmap",
                "absorption",
                "volume_climax_reversal",
            )
        )
    if (
        (funding_rate is not None and abs(funding_rate) >= 0.0004)
        or (basis_pct is not None and abs(basis_pct) >= 0.08)
        or (oi_change_pct is not None and abs(oi_change_pct) >= 1.5)
    ):
        fits.append("funding_reversal")
        fits.append("ls_ratio_extreme")
        fits.append("oi_divergence")

    if top_liquidity and liquid_enough and spread_ok:
        fits.extend(
            (
                "liquidity_sweep",
                "vwap_trend",
                "keltner_breakout",
                "whale_walls",
                "spread_strategy",
                "depth_imbalance",
            )
        )
        fits.extend(_PRICE_ACTION_COVERAGE_SETUP_IDS)

    if symbol in pinned_set or priority_asset:
        fits.extend(_ALL_SETUP_IDS)

    if not fits and spread_ok and quote_volume >= volume_floor:
        fits.extend(
            (
                "structure_pullback",
                "vwap_trend",
                "fvg_setup",
                "cvd_divergence",
                "price_velocity",
                "multi_tf_trend",
                "spread_strategy",
            )
        )
    # unconditional safety net: any symbol that reached this function gets a minimal set
    if not fits:
        fits.extend(
            (
                "structure_pullback",
                "vwap_trend",
                "fvg_setup",
                "cvd_divergence",
                "price_velocity",
            )
        )

    market_context = {
        "symbol": symbol,
        "base_asset": str(row.get("base_asset") or "").strip().upper(),
        "liquidity_rank": liquidity_rank,
        "shortlist_limit": int(getattr(settings.universe, "shortlist_limit", 50)),
        "quote_volume": quote_volume,
        "price_change_pct": price_change_pct,
        "spread_bps": spread_bps,
        "book_age_seconds": _safe_float(row.get("book_age_seconds")),
        "funding_rate": funding_rate,
        "oi_current": _safe_float(row.get("oi_current")),
        "oi_change_pct": oi_change_pct,
        "taker_ratio": _safe_float(row.get("taker_ratio")),
        "liquidation_score": _safe_float(row.get("liquidation_score")),
        "premium_zscore_5m": _safe_float(row.get("premium_zscore_5m")),
        "premium_slope_5m": _safe_float(row.get("premium_slope_5m")),
    }
    setups_config = getattr(settings, "setups", None)
    if setups_config is not None and hasattr(setups_config, "enabled_setup_ids"):
        enabled = set(setups_config.enabled_setup_ids())
    else:
        enabled = set(_ALL_SETUP_IDS)
    if not enabled:
        enabled = set(_ALL_SETUP_IDS)

    heuristic = tuple(
        setup_id
        for setup_id in dict.fromkeys(fits)
        if setup_id in enabled
        and calculate_strategy_fit_score(
            symbol,
            setup_id,
            market_context,
            settings=settings,
        )
        > 0.0
    )
    if symbol in pinned_set or priority_asset:
        return asset_strategy_allowlist(
            symbol,
            settings=settings,
            enabled=enabled,
            heuristic_fits=heuristic or tuple(fits),
        )
    # score-filter wiped all candidates but fits is non-empty → return enabled ∩ fits
    # so strategy routing is never empty for shortlisted symbols
    if not heuristic and fits:
        return tuple(s for s in dict.fromkeys(fits) if s in enabled)
    return heuristic


def strategy_fits_for_market_row(
    row: dict[str, Any],
    *,
    settings: BotSettings | None = None,
    liquidity_rank: int | None = None,
) -> tuple[str, ...]:
    """Return strategy routing fits using the same logic as production shortlist builds.

    Live audit tools and fallback analyzers construct synthetic ``UniverseSymbol``
    objects from REST rows. If they leave ``strategy_fits`` empty, downstream logs
    look like routing is broken even though production shortlist rows are healthy.
    This public wrapper keeps those tools on the production scoring path.
    """
    if settings is None:
        existing = row.get("strategy_fits")
        if isinstance(existing, (list, tuple)):
            return tuple(str(item) for item in existing if str(item).strip())
        return ()
    return _strategy_fits_for_row(row, settings=settings, liquidity_rank=liquidity_rank)


def _spread_freshness_score(row: dict[str, Any], settings: BotSettings) -> float:
    universe = settings.universe
    max_spread = float(getattr(universe, "shortlist_spread_max_bps", 8.0))
    stale_s = float(getattr(universe, "shortlist_book_stale_seconds", 90.0))
    spread_bps = _safe_float(row.get("spread_bps"))
    book_age = _safe_float(row.get("book_age_seconds"))
    mark_age = _safe_float(row.get("mark_price_age_seconds"))
    ticker_age = _safe_float(row.get("ticker_age_seconds"))

    spread_score = 0.55
    if spread_bps is not None and spread_bps > 0.0:
        spread_score = _clamp(1.0 - (spread_bps / max_spread))

    freshness_values = [
        _clamp(1.0 - (age / stale_s)) for age in (ticker_age, book_age, mark_age) if age is not None
    ]
    freshness_score = sum(freshness_values) / len(freshness_values) if freshness_values else 0.55
    return round(spread_score * 0.55 + freshness_score * 0.45, 6)


def _composite_score(
    *,
    row: dict[str, Any],
    settings: BotSettings,
    liquidity_rank: int,
    eligible_count: int,
    min_onboard_ms: int,
    outcome_penalty: float = 0.0,
) -> tuple[float, tuple[str, ...]]:
    shortlist_bucket = _bucket_for_price_change(float(row.get("price_change_percent") or 0.0))
    priority_asset = _is_priority_asset(row, settings)
    liquidity_curve = 1.0 - ((liquidity_rank - 1) / max(eligible_count - 1, 1))
    volume_floor = max(float(getattr(settings.universe, "min_quote_volume_usd", 0.0)), 1.0)
    volume = float(row.get("quote_volume") or 0.0)
    liquidity_depth = _clamp((math.log10(max(volume, 1.0)) - math.log10(volume_floor)) / 2.0 + 0.5)
    liquidity_score = round(liquidity_curve * 0.7 + liquidity_depth * 0.3, 6)

    onboard_date_ms = int(row.get("onboard_date_ms") or 0)
    age_score = 0.55
    if onboard_date_ms > 0:
        age_days = max((min_onboard_ms - onboard_date_ms) / 86_400_000.0, 0.0)
        age_score = _clamp(
            age_days / max(float(settings.universe.min_listing_age_days) * 5.0, 30.0)
        )

    move = abs(float(row.get("price_change_percent") or 0.0))
    if shortlist_bucket == "trend":
        bucket_fit = max(0.0, 1.0 - min(move, 2.0) / 2.0)
    elif shortlist_bucket == "breakout":
        bucket_fit = max(0.0, 1.0 - min(abs(move - 4.5) / 4.5, 1.0))
    else:
        bucket_fit = max(0.0, 1.0 - min(abs(move - 11.0) / 12.0, 1.0))

    tradability_score = 1.0
    if (
        row.get("status") != "TRADING"
        or str(row.get("contract_type") or "").upper() not in SUPPORTED_USDM_CONTRACT_TYPES
    ) or float(row.get("last_price") or 0.0) <= 0.0:
        tradability_score = 0.0
    else:
        tradability_score = 0.75 + bucket_fit * 0.25

    freshness_score = _spread_freshness_score(row, settings)
    oi_score = _oi_participation_score(row)
    sanity_score = _funding_basis_sanity_score(row, settings)
    crowding_score = _crowding_score(row)
    micro_score = _microstructure_opportunity_score(row)
    components = {
        "liquidity_score": liquidity_score,
        "age_score": age_score,
        "tradability_score": tradability_score,
        "freshness_score": freshness_score,
        "oi_score": oi_score,
        "sanity_score": sanity_score,
        "crowding_score": crowding_score,
        "micro_score": micro_score,
    }
    for name, value in list(components.items()):
        if not (0.0 <= value <= 1.0):
            LOG.warning(
                "composite_score_component_out_of_range",
                extra={"component": name, "value": value},
            )
            components[name] = max(0.0, min(1.0, value))
    liquidity_score = components["liquidity_score"]
    age_score = components["age_score"]
    freshness_score = components["freshness_score"]
    oi_score = components["oi_score"]
    sanity_score = components["sanity_score"]
    crowding_score = components["crowding_score"]
    micro_score = components["micro_score"]

    score = (
        components["liquidity_score"] * 0.29
        + components["age_score"] * 0.12
        + components["tradability_score"] * 0.18
        + components["freshness_score"] * 0.14
        + components["oi_score"] * 0.10
        + components["sanity_score"] * 0.08
        + components["crowding_score"] * 0.05
        + components["micro_score"] * 0.04
    )
    if priority_asset:
        score = max(score + 0.055, 0.70)

    reasons: list[str] = [f"bucket:{shortlist_bucket}"]
    if priority_asset:
        reasons.append("priority_deep_analysis")
    if liquidity_rank <= 10:
        reasons.append(f"liquidity_rank:{liquidity_rank}")
    if freshness_score >= 0.72:
        reasons.append("spread_freshness_strong")
    if oi_score >= 0.72:
        reasons.append("oi_participation_strong")
    if sanity_score >= 0.72:
        reasons.append("funding_basis_sane")
    if crowding_score <= 0.35:
        reasons.append("crowding_penalty")
    if micro_score >= 0.72:
        reasons.append("microstructure_active")
    if age_score >= 0.8:
        reasons.append("seasoned_listing")
    penalty = max(0.0, float(outcome_penalty))
    if penalty > 0.0:
        score = max(0.0, score - penalty)
        reasons.append(f"outcome_derank:-{penalty:.3f}")
    return round(min(score, 1.0), 6), tuple(reasons[:6])


def _prescore_row(
    row: dict[str, Any],
    settings: BotSettings,
    *,
    outcome_penalty: float = 0.0,
) -> float:
    """Lightweight score for stage-1 funnel before liquidity_rank is known."""
    volume_floor = max(float(getattr(settings.universe, "min_quote_volume_usd", 0.0)), 1.0)
    volume = float(row.get("quote_volume") or 0.0)
    liquidity_depth = _clamp((math.log10(max(volume, 1.0)) - math.log10(volume_floor)) / 2.0 + 0.5)
    freshness = _spread_freshness_score(row, settings)
    oi_score = _oi_participation_score(row)
    micro_score = _microstructure_opportunity_score(row)
    sanity_score = _funding_basis_sanity_score(row, settings)
    wash_score = _wash_volume_score(row)
    score = (
        liquidity_depth * 0.32
        + freshness * 0.20
        + oi_score * 0.16
        + sanity_score * 0.11
        + micro_score * 0.12
        + wash_score * 0.09
    )
    if _is_priority_asset(row, settings):
        score = max(score + 0.06, 0.72)
    radar_boost = _safe_float(row.get("radar_prescore_boost"), 0.0) or 0.0
    if radar_boost > 0.0:
        score = min(1.0, score + float(radar_boost))
    penalty = max(0.0, float(outcome_penalty))
    if penalty > 0.0:
        score = max(0.0, score - penalty)
    return round(min(score, 1.0), 6)


def _prescore_basis_candidates(
    rows: list[dict[str, Any]],
    *,
    settings: BotSettings,
    limit: int,
) -> list[dict[str, Any]]:
    missing = [row for row in rows if _safe_float(row.get("basis_pct")) is None]
    missing.sort(
        key=lambda item: (
            _prescore_row(item, settings),
            float(item.get("quote_volume") or 0.0),
        ),
        reverse=True,
    )
    return missing[: max(0, int(limit))]


def warm_prescore_basis_rows(
    rows: list[dict[str, Any]],
    *,
    settings: BotSettings,
    limit: int = DEFAULT_PRESCORE_BASIS_WARM_LIMIT,
    get_cached_basis: Callable[[str], float | None] | None = None,
    get_mark_basis: Callable[[str], float | None] | None = None,
) -> dict[str, int]:
    """Fill basis_pct for top prescore rows from WS mark/index or REST cache."""
    candidates = _prescore_basis_candidates(rows, settings=settings, limit=limit)
    ws_filled = 0
    cache_filled = 0
    for row in candidates:
        if _safe_float(row.get("basis_pct")) is not None:
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        basis: float | None = None
        source: str | None = None
        if get_mark_basis is not None:
            basis = get_mark_basis(symbol)
            if basis is not None:
                source = "ws"
        if basis is None and get_cached_basis is not None:
            basis = get_cached_basis(symbol)
            if basis is not None:
                source = "cache"
        if basis is None:
            continue
        row["basis_pct"] = float(basis)
        if source == "ws":
            ws_filled += 1
        else:
            cache_filled += 1
    still_missing = sum(1 for row in candidates if _safe_float(row.get("basis_pct")) is None)
    return {
        "basis_warm_candidates": len(candidates),
        "basis_warm_ws_filled": ws_filled,
        "basis_warm_cache_filled": cache_filled,
        "basis_warm_still_missing": still_missing,
    }


async def warm_prescore_basis_rest(
    rows: list[dict[str, Any]],
    fetch_basis: Callable[[str], Awaitable[float | None]],
    *,
    settings: BotSettings,
    limit: int = DEFAULT_PRESCORE_BASIS_WARM_LIMIT,
) -> dict[str, int]:
    """Bounded REST basis warmup for prescore candidates still missing basis_pct."""
    candidates = _prescore_basis_candidates(rows, settings=settings, limit=limit)
    attempted = 0
    ok = 0
    failed = 0
    for row in candidates:
        if _safe_float(row.get("basis_pct")) is not None:
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        attempted += 1
        try:
            basis = await fetch_basis(symbol)
        except DEFENSIVE_EXC:
            failed += 1
            continue
        if basis is None:
            failed += 1
            continue
        row["basis_pct"] = float(basis)
        ok += 1
    return {
        "basis_warm_attempted": attempted,
        "basis_warm_ok": ok,
        "basis_warm_failed": failed,
    }


def select_light_pool_rows(
    gate_passed_rows: list[dict[str, Any]],
    *,
    settings: BotSettings,
    pinned: set[str],
    priority_symbols: set[str],
    outcome_penalties: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep top ``light_pool_limit`` by composite prescore plus protected pins/priority."""
    light_pool_limit = int(getattr(settings.universe, "light_pool_limit", 180) or 180)
    gate_passed = len(gate_passed_rows)
    if not gate_passed_rows:
        return [], {
            "gate_passed": 0,
            "light_pool": 0,
            "light_pool_limit": light_pool_limit,
        }

    protected = pinned | priority_symbols
    penalties = outcome_penalties or {}
    max_spread = float(getattr(settings.universe, "shortlist_spread_max_bps", 8.0))
    spread_rejected = 0
    eligible_rows: list[dict[str, Any]] = []
    for row in gate_passed_rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        spread_bps = _safe_float(row.get("spread_bps"))
        if spread_bps is not None and spread_bps > max_spread and symbol not in protected:
            spread_rejected += 1
            continue
        eligible_rows.append(row)

    def _row_prescore(item: dict[str, Any]) -> float:
        symbol = str(item.get("symbol") or "").strip().upper()
        penalty = float(penalties.get(symbol, 0.0))
        return _prescore_row(item, settings, outcome_penalty=penalty)

    scored_rows = sorted(
        eligible_rows,
        key=lambda item: (
            _row_prescore(item),
            float(item.get("quote_volume") or 0.0),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in scored_rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or symbol not in protected or symbol in seen:
            continue
        selected.append(row)
        seen.add(symbol)

    for row in scored_rows:
        if len(selected) >= light_pool_limit:
            break
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        selected.append(row)
        seen.add(symbol)

    selected.sort(
        key=lambda item: (
            _row_prescore(item),
            float(item.get("quote_volume") or 0.0),
        ),
        reverse=True,
    )
    stats = {
        "gate_passed": gate_passed,
        "light_pool": len(selected),
        "light_pool_limit": light_pool_limit,
        "spread_gate_rejected": spread_rejected,
    }
    if gate_passed > light_pool_limit:
        LOG.debug(
            "light_pool_funnel | gate_passed=%d light_pool=%d limit=%d",
            gate_passed,
            len(selected),
            light_pool_limit,
        )
    return selected, stats


def build_shortlist(
    symbol_meta: list[SymbolMeta],
    tickers_24h: list[dict[str, float | str]],
    settings: BotSettings,
    *,
    seed_source: str = "rest_full",
    market_regime: str | None = None,
    outcome_penalties: dict[str, float] | None = None,
    get_cached_basis: Callable[[str], float | None] | None = None,
    get_mark_basis: Callable[[str], float | None] | None = None,
    prescore_basis_warm_limit: int | None = None,
) -> tuple[list[UniverseSymbol], dict[str, Any]]:
    shortlist_limit = int(getattr(settings.universe, "shortlist_limit", 50))
    if not symbol_meta or not tickers_24h:
        LOG.warning("build_shortlist_empty_input")
        return [], {
            "reason": "empty_input",
            "symbol_meta_count": len(symbol_meta or []),
            "ticker_count": len(tickers_24h or []),
            "seed_source": seed_source,
        }
    if shortlist_limit < 10:
        LOG.warning(
            "shortlist_limit=%d is very small - check config.toml [universe] section",
            shortlist_limit,
        )
    meta_map = {meta.symbol: meta for meta in symbol_meta}
    pinned = set(settings.universe.pinned_symbols)
    priority_symbols = _priority_symbols(settings)
    min_onboard = datetime.now(UTC) - timedelta(days=settings.universe.min_listing_age_days)
    min_onboard_ms = int(min_onboard.timestamp() * 1000)
    eligible_rows: list[dict[str, Any]] = []

    for ticker_row in tickers_24h:
        symbol = str(ticker_row.get("symbol") or "").strip().upper()
        meta = meta_map.get(symbol)
        if meta is None:
            continue
        if not _is_supported_contract_symbol(symbol, meta.base_asset.upper()):
            continue
        if meta.status.upper() != "TRADING":
            continue
        if meta.contract_type.upper() not in SUPPORTED_USDM_CONTRACT_TYPES:
            continue
        if meta.quote_asset.upper() != settings.universe.quote_asset:
            continue
        if meta.base_asset.upper() in STABLE_BASE_ASSETS:
            continue
        quote_volume = float(ticker_row.get("quote_volume") or 0.0)
        last_price = float(ticker_row.get("last_price") or 0.0)
        price_change_pct = float(ticker_row.get("price_change_percent") or 0.0)
        trade_count = int(float(ticker_row.get("trade_count") or 0.0))
        if quote_volume <= 0.0 or last_price <= 0.0:
            continue
        protected_symbol = symbol in pinned or symbol in priority_symbols
        if not protected_symbol:
            if quote_volume < settings.universe.min_quote_volume_usd:
                continue
            if abs(price_change_pct) < settings.universe.min_price_change_pct:
                continue
            if trade_count < settings.universe.min_trade_count_24h:
                continue
            if meta.onboard_date_ms and meta.onboard_date_ms > min_onboard_ms:
                continue
        eligible_rows.append(
            {
                "symbol": symbol,
                "base_asset": meta.base_asset,
                "quote_asset": meta.quote_asset,
                "contract_type": meta.contract_type,
                "status": meta.status,
                "onboard_date_ms": meta.onboard_date_ms,
                "quote_volume": quote_volume,
                "price_change_pct": price_change_pct,
                "trade_count": trade_count,
                "last_price": last_price,
                "shortlist_bucket": _bucket_for_price_change(price_change_pct),
                "spread_bps": _safe_float(ticker_row.get("spread_bps")),
                "ticker_age_seconds": _safe_float(ticker_row.get("ticker_age_seconds")),
                "book_age_seconds": _safe_float(ticker_row.get("book_age_seconds")),
                "mark_price_age_seconds": _safe_float(ticker_row.get("mark_price_age_seconds")),
                "oi_change_pct": _safe_float(ticker_row.get("oi_change_pct")),
                "oi_current": _safe_float(ticker_row.get("oi_current")),
                "funding_rate": _safe_float(ticker_row.get("funding_rate")),
                "basis_pct": _safe_float(ticker_row.get("basis_pct")),
                "top_account_ls_ratio": _safe_float(ticker_row.get("top_account_ls_ratio")),
                "top_position_ls_ratio": _safe_float(ticker_row.get("top_position_ls_ratio")),
                "global_account_ls_ratio": _safe_float(ticker_row.get("global_account_ls_ratio")),
                "top_vs_global_ls_gap": _safe_float(ticker_row.get("top_vs_global_ls_gap")),
                "taker_ratio": _safe_float(ticker_row.get("taker_ratio")),
                "liquidation_score": _safe_float(ticker_row.get("liquidation_score")),
                "premium_slope_5m": _safe_float(ticker_row.get("premium_slope_5m")),
                "premium_zscore_5m": _safe_float(ticker_row.get("premium_zscore_5m")),
                "funding_trend": ticker_row.get("funding_trend"),
            }
        )

    penalties = outcome_penalties or {}
    light_pool_rows, funnel_stats = select_light_pool_rows(
        eligible_rows,
        settings=settings,
        pinned=pinned,
        priority_symbols=priority_symbols,
        outcome_penalties=penalties,
    )
    light_pool_rows.sort(
        key=lambda item: (
            _prescore_row(
                item,
                settings,
                outcome_penalty=float(penalties.get(str(item["symbol"]).upper(), 0.0)),
            ),
            float(item["quote_volume"]),
        ),
        reverse=True,
    )
    warm_limit = int(
        prescore_basis_warm_limit
        or getattr(
            settings.universe, "prescore_basis_warm_limit", DEFAULT_PRESCORE_BASIS_WARM_LIMIT
        )
        or DEFAULT_PRESCORE_BASIS_WARM_LIMIT
    )
    basis_warm = warm_prescore_basis_rows(
        light_pool_rows,
        settings=settings,
        limit=warm_limit,
        get_cached_basis=get_cached_basis,
        get_mark_basis=get_mark_basis,
    )
    eligible: list[UniverseSymbol] = []
    liquidity_rank = 0
    previous_volume: float | None = None
    derank_applied = 0
    for index, el_row in enumerate(light_pool_rows, start=1):
        row_volume = float(el_row["quote_volume"])
        if previous_volume is None or not math.isclose(
            row_volume, previous_volume, rel_tol=0.0, abs_tol=1e-9
        ):
            liquidity_rank = index
            previous_volume = row_volume
        symbol_key = str(el_row["symbol"]).upper()
        penalty = float(penalties.get(symbol_key, 0.0))
        if penalty > 0.0:
            derank_applied += 1
        shortlist_score, reasons = _composite_score(
            row=el_row,
            settings=settings,
            liquidity_rank=liquidity_rank,
            eligible_count=len(light_pool_rows),
            min_onboard_ms=min_onboard_ms,
            outcome_penalty=penalty,
        )
        eligible.append(
            UniverseSymbol(
                symbol=str(el_row["symbol"]),
                base_asset=str(el_row["base_asset"]),
                quote_asset=str(el_row["quote_asset"]),
                contract_type=str(el_row["contract_type"]),
                status=str(el_row["status"]),
                onboard_date_ms=int(el_row["onboard_date_ms"]),
                quote_volume=float(el_row["quote_volume"]),
                price_change_pct=float(el_row["price_change_pct"]),
                last_price=float(el_row["last_price"]),
                trade_count_24h=int(el_row["trade_count"]),
                shortlist_bucket=str(el_row["shortlist_bucket"]),
                shortlist_score=shortlist_score,
                shortlist_reasons=reasons,
                seed_source=seed_source,
                liquidity_rank=liquidity_rank,
                strategy_fits=_strategy_fits_for_row(
                    el_row,
                    settings=settings,
                    liquidity_rank=liquidity_rank,
                ),
            )
        )

    eligible.sort(
        key=lambda item: (
            item.shortlist_score or 0.0,
            _bucket_priority(item)[0],
            item.quote_volume,
            item.symbol,
        ),
        reverse=True,
    )
    pinned_rows = [p_row for p_row in eligible if p_row.symbol in pinned]
    priority_rows = [
        p_row
        for p_row in eligible
        if p_row.symbol in priority_symbols and p_row.symbol not in pinned
    ]
    dynamic_candidates = [d_row for d_row in eligible if d_row.symbol not in pinned]
    dynamic_candidates.sort(
        key=lambda item: (
            item.shortlist_score or 0.0,
            _bucket_priority(item)[0],
            item.quote_volume,
            item.symbol,
        ),
        reverse=True,
    )
    dynamic_pool = dynamic_candidates[: settings.universe.dynamic_limit]
    bucket_pool: dict[str, list[UniverseSymbol]] = {
        "trend": [],
        "breakout": [],
        "reversal": [],
    }
    for pool_row in dynamic_pool:
        bucket_pool[pool_row.shortlist_bucket].append(pool_row)
    for bucket in bucket_pool.values():
        bucket.sort(
            key=lambda item: (
                item.shortlist_score or 0.0,
                _bucket_priority(item)[0],
                item.quote_volume,
                item.symbol,
            ),
            reverse=True,
        )

    shortlist: list[UniverseSymbol] = []
    seen: set[str] = set()
    for s_row in pinned_rows:
        if s_row.symbol in seen:
            continue
        shortlist.append(s_row)
        seen.add(s_row.symbol)
    for pr_row in priority_rows:
        if pr_row.symbol in seen or len(shortlist) >= shortlist_limit:
            continue
        shortlist.append(pr_row)
        seen.add(pr_row.symbol)

    targets = _scaled_bucket_targets(
        max(shortlist_limit - len(shortlist), 0),
        market_regime=market_regime,
    )
    summary: dict[str, Any] = {
        "mode": seed_source,
        "market_regime": market_regime,
        "gate_passed": funnel_stats["gate_passed"],
        "light_pool": funnel_stats["light_pool"],
        "light_pool_limit": funnel_stats["light_pool_limit"],
        "eligible": len(eligible),
        "dynamic_pool": len(dynamic_pool),
        "pinned": len(pinned_rows),
        "priority": len([item for item in eligible if item.symbol in priority_symbols]),
        "trend": 0,
        "breakout": 0,
        "reversal": 0,
        "fill": 0,
        "strategy_seed": 0,
        "outcome_derank_applied": derank_applied,
        "basis_warm": basis_warm,
        "avg_score": round(
            sum((summ_row.shortlist_score or 0.0) for summ_row in shortlist)
            / max(len(shortlist), 1),
            6,
        ),
    }

    for b_name in ("trend", "breakout", "reversal"):
        for b_row in bucket_pool[b_name]:
            if len(shortlist) >= shortlist_limit or cast("int", summary[b_name]) >= targets[b_name]:
                break
            if b_row.symbol in seen:
                continue
            if not b_row.strategy_fits:
                continue
            shortlist.append(b_row)
            seen.add(b_row.symbol)
            summary[b_name] = cast("int", summary[b_name]) + 1

    pool_summary = fill_shortlist_from_pools(
        shortlist=shortlist,
        seen=seen,
        dynamic_pool=dynamic_pool,
        shortlist_limit=shortlist_limit,
        setup_ids=_ALL_SETUP_IDS,
        market_regime=market_regime,
    )
    for key, value in pool_summary.items():
        if key in summary:
            summary[key] = cast("int", summary[key]) + int(value)
        else:
            summary[key] = value

    shortlist.sort(
        key=lambda item: (
            item.symbol not in pinned,
            -(item.shortlist_score or 0.0),
            -_bucket_priority(item)[0],
            -item.quote_volume,
            item.symbol,
        )
    )
    summary["avg_score"] = round(
        sum((avg_row.shortlist_score or 0.0) for avg_row in shortlist) / max(len(shortlist), 1),
        6,
    )
    strategy_counts = dict.fromkeys(_ALL_SETUP_IDS, 0)
    for count_row in shortlist:
        for setup_id in count_row.strategy_fits:
            if setup_id in strategy_counts:
                strategy_counts[setup_id] += 1
    summary["strategy_fit_counts"] = {
        key: value for key, value in strategy_counts.items() if value > 0
    }
    scores = [float(item.shortlist_score) for item in shortlist if item.shortlist_score is not None]
    if scores:
        scores_sorted = sorted(scores)
        n = len(scores_sorted)

        def pct(p: int) -> float:
            idx = max(0, min(n - 1, int(n * p / 100)))
            return round(scores_sorted[idx], 6)

        summary["score_p25"] = pct(25)
        summary["score_p50"] = pct(50)
        summary["score_p75"] = pct(75)
        summary["score_p90"] = pct(90)
        summary["strategy_fit_density"] = round(
            sum(len(item.strategy_fits) for item in shortlist) / max(len(shortlist), 1),
            2,
        )
    return shortlist, summary


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    fraction = max(0.0, min(float(fraction), 1.0))
    if len(values) == 1:
        return round(values[0], 6)
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(values[lower], 6)
    weight = position - lower
    return round(values[lower] * (1.0 - weight) + values[upper] * weight, 6)


def rerank_shortlist(
    current_shortlist: list[UniverseSymbol],
    latest_tickers: list[dict[str, Any]],
    settings: BotSettings,
    *,
    outcome_penalties: dict[str, float] | None = None,
) -> list[UniverseSymbol]:
    """Fast WebSocket-based reranking of an existing shortlist.

    Does not add/remove symbols, only updates their metrics and resorts based
    on real-time activity (volume and volatility).
    """
    ticker_map = {str(t.get("symbol", "")).upper(): t for t in latest_tickers if t.get("symbol")}
    updated_rows: list[tuple[UniverseSymbol, dict[str, Any]]] = []

    for item in current_shortlist:
        ticker = ticker_map.get(item.symbol)
        row = {
            "symbol": item.symbol,
            "base_asset": item.base_asset,
            "quote_asset": item.quote_asset,
            "contract_type": item.contract_type,
            "status": item.status,
            "onboard_date_ms": item.onboard_date_ms,
            "quote_volume": item.quote_volume,
            "price_change_pct": item.price_change_pct,
            "price_change_percent": item.price_change_pct,
            "last_price": item.last_price,
        }
        if ticker:
            # Update dynamic metrics
            new_volume = _safe_float(ticker.get("quote_volume"), item.quote_volume)
            if new_volume is None or new_volume <= 0.0:
                new_volume = item.quote_volume
            new_change = _safe_float(ticker.get("price_change_percent"), item.price_change_pct)
            if new_change is None:
                new_change = item.price_change_pct
            new_price = _safe_float(ticker.get("last_price"), item.last_price)
            if new_price is None or new_price <= 0.0:
                new_price = item.last_price
            row.update(
                {
                    "quote_volume": new_volume,
                    "price_change_pct": new_change,
                    "price_change_percent": new_change,
                    "last_price": new_price,
                    "spread_bps": _safe_float(ticker.get("spread_bps")),
                    "ticker_age_seconds": _safe_float(ticker.get("ticker_age_seconds")),
                    "book_age_seconds": _safe_float(ticker.get("book_age_seconds")),
                    "mark_price_age_seconds": _safe_float(ticker.get("mark_price_age_seconds")),
                    "oi_change_pct": _safe_float(ticker.get("oi_change_pct")),
                    "oi_current": _safe_float(ticker.get("oi_current")),
                    "funding_rate": _safe_float(ticker.get("funding_rate")),
                    "basis_pct": _safe_float(ticker.get("basis_pct")),
                    "top_account_ls_ratio": _safe_float(ticker.get("top_account_ls_ratio")),
                    "top_position_ls_ratio": _safe_float(ticker.get("top_position_ls_ratio")),
                    "global_account_ls_ratio": _safe_float(ticker.get("global_account_ls_ratio")),
                    "top_vs_global_ls_gap": _safe_float(ticker.get("top_vs_global_ls_gap")),
                    "taker_ratio": _safe_float(ticker.get("taker_ratio")),
                    "liquidation_score": _safe_float(ticker.get("liquidation_score")),
                    "premium_slope_5m": _safe_float(ticker.get("premium_slope_5m")),
                    "premium_zscore_5m": _safe_float(ticker.get("premium_zscore_5m")),
                    "funding_trend": ticker.get("funding_trend"),
                }
            )

        updated_rows.append((item, row))

    ranked_rows = sorted(
        updated_rows,
        key=lambda pair: float(pair[1].get("quote_volume") or 0.0),
        reverse=True,
    )
    rank_by_symbol: dict[str, int] = {}
    previous_volume: float | None = None
    liquidity_rank = 0
    for index, (item, row) in enumerate(ranked_rows, start=1):
        volume = row_float(row, "quote_volume")
        if previous_volume is None or not math.isclose(
            volume, previous_volume, rel_tol=0.0, abs_tol=1e-9
        ):
            liquidity_rank = index
            previous_volume = volume
        rank_by_symbol[item.symbol] = liquidity_rank

    min_onboard_ms = int(
        (datetime.now(UTC) - timedelta(days=settings.universe.min_listing_age_days)).timestamp()
        * 1000
    )
    penalties = outcome_penalties or {}
    updated: list[UniverseSymbol] = []
    for item, row in updated_rows:
        liquidity_rank = rank_by_symbol.get(item.symbol) or item.liquidity_rank or 1
        penalty = float(penalties.get(item.symbol.upper(), 0.0))
        shortlist_score, reasons = _composite_score(
            row=row,
            settings=settings,
            liquidity_rank=liquidity_rank,
            eligible_count=max(len(updated_rows), 1),
            min_onboard_ms=min_onboard_ms,
            outcome_penalty=penalty,
        )
        updated.append(
            replace(
                item,
                quote_volume=row_float(row, "quote_volume", float(item.quote_volume)),
                price_change_pct=row_float(
                    row, "price_change_percent", float(item.price_change_pct)
                ),
                last_price=row_float(row, "last_price", float(item.last_price)),
                shortlist_bucket=_bucket_for_price_change(
                    row_float(row, "price_change_percent", float(item.price_change_pct))
                ),
                shortlist_score=shortlist_score,
                shortlist_reasons=reasons,
                liquidity_rank=liquidity_rank,
                strategy_fits=_strategy_fits_for_row(
                    row,
                    settings=settings,
                    liquidity_rank=liquidity_rank,
                ),
            )
        )

    pinned = set(settings.universe.pinned_symbols)
    return sorted(
        updated,
        key=lambda item: (
            item.symbol not in pinned,
            -(item.shortlist_score or 0.0),
            -_bucket_priority(item)[0],
            -item.quote_volume,
            item.symbol,
        ),
    )

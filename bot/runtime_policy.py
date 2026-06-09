"""Runtime policy helpers shared by live routing, filters, and setups."""

from __future__ import annotations

from typing import Any

_VALID_TIMEFRAMES = {"5m", "15m", "1h", "4h"}


def asset_config_for_symbol(settings: Any | None, symbol: str) -> Any | None:
    """Return the configured per-asset policy, if one exists."""
    assets = getattr(settings, "assets", None)
    if not isinstance(assets, dict):
        return None
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return None
    return assets.get(normalized)


def configured_primary_timeframe(
    settings: Any | None,
    symbol: str,
    *,
    default: str = "15m",
) -> str:
    """Resolve a validated primary timeframe for a symbol."""
    asset_config = asset_config_for_symbol(settings, symbol)
    raw = getattr(asset_config, "primary_timeframe", default)
    timeframe = str(raw or default).strip().lower()
    return timeframe if timeframe in _VALID_TIMEFRAMES else default


def configured_context_timeframes(
    settings: Any | None,
    symbol: str,
    *,
    default: tuple[str, ...] = ("1h", "4h"),
) -> tuple[str, ...]:
    """Resolve validated context timeframes for telemetry and policy decisions."""
    asset_config = asset_config_for_symbol(settings, symbol)
    raw_values = getattr(asset_config, "context_timeframes", default)
    values: list[str] = []
    for raw in raw_values or ():
        value = str(raw or "").strip().lower()
        if value in _VALID_TIMEFRAMES and value not in values:
            values.append(value)
    return tuple(values or default)


def is_deep_analysis_symbol(prepared_or_symbol: Any, settings: Any | None = None) -> bool:
    """Return True only for symbols explicitly configured for deep live analysis."""
    symbol = getattr(prepared_or_symbol, "symbol", prepared_or_symbol)
    resolved_settings = settings or getattr(prepared_or_symbol, "settings", None)
    asset_config = asset_config_for_symbol(resolved_settings, str(symbol or ""))
    return bool(getattr(asset_config, "deep_analysis", False))


def effective_engine_score_floor(
    settings: Any,
    *,
    prepared_or_symbol: Any | None = None,
) -> float:
    """Minimum score for engine best-signal selection (delivery-aligned)."""
    delivery = getattr(settings, "delivery", None)
    filters = getattr(settings, "filters", None)
    watch_min = float(getattr(delivery, "watch_min_score", 0.0) or 0.0)
    filter_min = float(getattr(filters, "min_score", 0.0) or 0.0)
    positives = [value for value in (watch_min, filter_min) if value > 0.0]
    floor = min(positives) if positives else 0.0

    if prepared_or_symbol is not None and is_deep_analysis_symbol(prepared_or_symbol, settings):
        symbol = getattr(prepared_or_symbol, "symbol", prepared_or_symbol)
        primary_timeframe = configured_primary_timeframe(settings, str(symbol or ""))
        deep_score_floor = 0.48 if primary_timeframe in {"1h", "4h"} else 0.50
        floor = min(floor, deep_score_floor) if floor > 0.0 else deep_score_floor
    return float(floor)


def resolve_setup_entry_tf(setup_id: str, *, default: str = "15m") -> str:
    """Catalog entry TF with optional future per-setup config override."""
    from bot.domain.strategy_catalog import CATALOG_BY_ID

    entry = CATALOG_BY_ID.get(str(setup_id or "").strip())
    if entry is None:
        return default
    return str(entry.trigger_tf or default).split("+")[0].split("/")[0].strip().lower() or default


def resolve_setup_order_type(setup_id: str, *, default: str = "limit") -> str:
    from bot.domain.strategy_catalog import resolve_setup_order_type as _catalog_order

    return _catalog_order(setup_id, default=default)


def resolve_setup_ttl_minutes(setup_id: str, *, default: int = 120) -> int:
    from bot.domain.strategy_catalog import resolve_setup_ttl_minutes as _catalog_ttl

    return _catalog_ttl(setup_id, default=default)


def resolve_setup_pattern_tf(setup_id: str, *, default: str = "15m") -> str:
    from bot.domain.strategy_catalog import CATALOG_BY_ID

    entry = CATALOG_BY_ID.get(str(setup_id or "").strip())
    if entry is None:
        return default
    raw = str(entry.pattern_tf or entry.trigger_tf or default)
    return raw.split("/", maxsplit=1)[0].split("+", maxsplit=1)[0].strip().lower() or default


def effective_shortlist_unified_routing(
    runtime: Any | None,
    *,
    shortlist_total: int = 0,
) -> bool:
    """True when unified shortlist routing is configured and the shortlist is non-empty."""
    if runtime is None:
        return False
    if not bool(getattr(runtime, "shortlist_unified_routing", False)):
        return False
    return int(shortlist_total) > 0

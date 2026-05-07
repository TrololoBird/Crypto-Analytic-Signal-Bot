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

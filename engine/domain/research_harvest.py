"""Research harvest mode - deep public data capture for strategy design (no Telegram)."""

from __future__ import annotations

from .config import REQUIRED_PINNED_SYMBOLS, AssetConfig, BotSettings, ResearchHarvestConfig

# Ten liquid USD-M symbols: seven benchmark pins + three high-beta alts.
DEFAULT_RESEARCH_HARVEST_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "XAUUSDT",
    "XAGUSDT",
    "PAXGUSDT",
)


def _resolved_symbols(rh: ResearchHarvestConfig) -> tuple[str, ...]:
    base = rh.symbols or DEFAULT_RESEARCH_HARVEST_SYMBOLS
    return tuple(dict.fromkeys((*base, *REQUIRED_PINNED_SYMBOLS)))


def activate_research_harvest(
    settings: BotSettings,
    *,
    symbols: tuple[str, ...] | None = None,
) -> BotSettings:
    """Enable harvest mode on a loaded ``BotSettings`` instance."""
    rh = settings.research_harvest
    patch: dict[str, object] = {"enabled": True}
    if symbols:
        patch["symbols"] = tuple(
            dict.fromkeys(str(item).strip().upper() for item in symbols if str(item).strip())
        )
    updated = settings.model_copy(
        deep=True,
        update={"research_harvest": rh.model_copy(update=patch)},
    )
    return apply_research_harvest_profile(updated)


def apply_research_harvest_profile(settings: BotSettings) -> BotSettings:
    """Apply runtime overrides when ``research_harvest.enabled`` is true."""
    rh = settings.research_harvest
    if not rh.enabled:
        return settings

    symbols = _resolved_symbols(rh)
    symbol_count = len(symbols)
    light_pool = max(symbol_count, 20)

    assets = dict(settings.assets)
    for symbol in symbols:
        existing = assets.get(symbol)
        if existing is None:
            assets[symbol] = AssetConfig(
                deep_analysis=True,
                primary_timeframe="15m",
                context_timeframes=("1h", "4h"),
            )
        else:
            assets[symbol] = existing.model_copy(update={"deep_analysis": True})

    return settings.model_copy(
        deep=True,
        update={
            "notifiers": settings.notifiers.model_copy(update={"provider": "none"}),
            "assets": assets,
            "universe": settings.universe.model_copy(
                update={
                    "pinned_symbols": symbols,
                    "shortlist_limit": symbol_count,
                    "dynamic_limit": symbol_count,
                    "light_pool_limit": light_pool,
                    "radar": settings.universe.radar.model_copy(update={"enabled": False}),
                }
            ),
            "runtime": settings.runtime.model_copy(
                update={
                    "route_all_enabled_strategies": rh.route_all_enabled_strategies,
                    "emit_strategy_routing_skips": rh.emit_strategy_routing_skips,
                    "shortlist_refresh_interval_seconds": 86_400,
                    "emergency_fallback_seconds": max(
                        settings.runtime.emergency_fallback_seconds, 3600
                    ),
                }
            ),
            "ws": settings.ws.model_copy(
                update={
                    "depth_symbol_limit": symbol_count,
                    "subscription_scope": "shortlist",
                }
            ),
            "delivery": settings.delivery.model_copy(
                update={
                    "action_cap_per_session": 0,
                    "watch_screener_enabled": True,
                }
            ),
            "spot_companion": settings.spot_companion.model_copy(
                update={
                    "enabled": rh.enable_spot_companion,
                    "lead_symbols": symbols[: min(8, symbol_count)],
                }
            ),
            "intelligence": settings.intelligence.model_copy(
                update={"benchmark_symbols": tuple(REQUIRED_PINNED_SYMBOLS)}
            ),
            "data_dir": settings.data_dir / "harvest",
        },
    )

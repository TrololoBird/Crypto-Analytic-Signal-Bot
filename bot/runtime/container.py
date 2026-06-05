"""Dependency container for SignalBot runtime."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.event_bus import EventBus
from ..delivery import SignalDelivery
from ..delivery.telegram import build_message_broadcaster
from ..delivery.watch import AlertCoordinator
from ..diagnostics.quality import SignalQualityMonitor
from ..engine import SignalEngine, StrategyRegistry
from ..market.data import BinanceFuturesMarketData, configure_rest_concurrency
from ..market.enrichment import PublicIntelligenceService
from ..market.proxy_bootstrap import ensure_network_ready
from ..market.radar_state import MarketRadarStore
from ..market.rest_impl import BinanceClientImpl
from ..market.ws import FuturesWSManager
from ..persistence.public_audit import PublicAuditLedger
from ..persistence.repository.memory import MemoryRepository
from ..persistence.tracking import SignalTracker
from ..telemetry import TelemetryStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..domain import BotSettings

LOG = logging.getLogger("bot.runtime.container")


@dataclass(slots=True)
class ApplicationContainer:
    """Initialized runtime dependencies for ``SignalBot``."""

    client: BinanceFuturesMarketData
    bus: EventBus
    ws_manager: FuturesWSManager | None
    telemetry: TelemetryStore
    telegram: Any
    delivery: SignalDelivery
    alerts: AlertCoordinator
    repository: MemoryRepository
    quality_monitor: SignalQualityMonitor
    tracker: SignalTracker
    intelligence: PublicIntelligenceService | None
    registry: StrategyRegistry
    engine: SignalEngine
    public_audit: PublicAuditLedger


async def build_application_container_async(
    settings: BotSettings,
    *,
    register_strategies: Callable[[StrategyRegistry], None],
    market_data: BinanceFuturesMarketData | None = None,
    broadcaster: Any | None = None,
    telemetry: TelemetryStore | None = None,
    feature_flags: Any | None = None,
    config_path: str | Path = "config.toml",
) -> ApplicationContainer:
    settings = await ensure_network_ready(settings, config_path=Path(config_path))
    return _build_application_container_impl(
        settings,
        register_strategies=register_strategies,
        market_data=market_data,
        broadcaster=broadcaster,
        telemetry=telemetry,
        feature_flags=feature_flags,
    )


def build_application_container(
    settings: BotSettings,
    *,
    register_strategies: Callable[[StrategyRegistry], None],
    market_data: BinanceFuturesMarketData | None = None,
    broadcaster: Any | None = None,
    telemetry: TelemetryStore | None = None,
    feature_flags: Any | None = None,
) -> ApplicationContainer:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            build_application_container_async(
                settings,
                register_strategies=register_strategies,
                market_data=market_data,
                broadcaster=broadcaster,
                telemetry=telemetry,
                feature_flags=feature_flags,
            )
        )
    LOG.info("network bootstrap deferred to SignalBot.start() (async event loop active)")
    return _build_application_container_impl(
        settings,
        register_strategies=register_strategies,
        market_data=market_data,
        broadcaster=broadcaster,
        telemetry=telemetry,
        feature_flags=feature_flags,
    )


def _build_application_container_impl(
    settings: BotSettings,
    *,
    register_strategies: Callable[[StrategyRegistry], None],
    market_data: BinanceFuturesMarketData | None = None,
    broadcaster: Any | None = None,
    telemetry: TelemetryStore | None = None,
    feature_flags: Any | None = None,
) -> ApplicationContainer:
    """Build runtime dependencies for ``SignalBot``.

    ``register_strategies`` is provided by ``SignalBot`` to keep strategy wiring in one place.
    """

    configure_rest_concurrency(settings.runtime.max_concurrent_rest_requests)

    binance_client = BinanceClientImpl(
        rest_timeout_seconds=settings.ws.rest_timeout_seconds,
        futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
        network=settings.network,
    )
    client = market_data or BinanceFuturesMarketData(binance_client=binance_client)

    bus = EventBus(
        max_size=settings.runtime.event_bus_max_size,
        warn_depth=settings.runtime.event_bus_warn_depth,
        drop_log_interval=settings.runtime.event_bus_drop_log_interval,
    )
    ws_manager: FuturesWSManager | None = None
    if settings.ws.enabled and isinstance(client, BinanceFuturesMarketData):
        ws_manager = FuturesWSManager(
            client,
            settings.ws,
            proxy_url=binance_client._proxy_url,
            trust_env=settings.network.trust_env,
        )
        ws_manager.set_event_bus(bus)
        if settings.universe.radar.enabled:
            radar_store = MarketRadarStore(
                settings.universe.radar,
                quote_asset=settings.universe.quote_asset,
            )
            ws_manager.set_radar_store(radar_store)
        if hasattr(client, "_ws"):
            client._ws = ws_manager
        binance_client._ws = ws_manager
        LOG.info(
            "ws_manager initialized | pinned_symbols=%d",
            len(settings.universe.pinned_symbols),
        )

    tg = broadcaster or build_message_broadcaster(settings)
    public_audit = PublicAuditLedger(
        settings.public_audit_dir,
        enabled=settings.delivery.public_audit_enabled,
    )
    delivery = SignalDelivery(
        tg,
        pending_expiry_minutes=settings.tracking.pending_expiry_minutes,
        public_audit=public_audit,
    )
    telemetry_store = telemetry or TelemetryStore(settings.telemetry_dir)
    alerts = AlertCoordinator(
        settings=settings,
        broadcaster=tg,
        telemetry=telemetry_store,
    )

    repository = MemoryRepository(
        db_path=settings.db_path,
        data_dir=settings.data_dir / "parquet",
    )
    quality_monitor = SignalQualityMonitor(persist_path=settings.data_dir / "quality_monitor.json")

    tracker = SignalTracker(
        settings,
        market_data=client,
        telemetry=telemetry_store,
        memory_repo=repository,
        quality_monitor=quality_monitor,
    )

    intelligence: PublicIntelligenceService | None = None
    if isinstance(client, BinanceFuturesMarketData):
        intelligence = PublicIntelligenceService(settings, client, telemetry_store)

    registry = StrategyRegistry()
    register_strategies(registry)
    engine = SignalEngine(registry, settings, feature_flags=feature_flags)

    return ApplicationContainer(
        client=client,
        bus=bus,
        ws_manager=ws_manager,
        telemetry=telemetry_store,
        telegram=tg,
        delivery=delivery,
        alerts=alerts,
        repository=repository,
        quality_monitor=quality_monitor,
        tracker=tracker,
        intelligence=intelligence,
        registry=registry,
        engine=engine,
        public_audit=public_audit,
    )

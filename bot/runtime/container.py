"""Dependency container for SignalBot runtime."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..delivery.watch import AlertCoordinator
from ..domain import BotSettings
from ..engine import SignalEngine, StrategyRegistry
from ..core.event_bus import EventBus
from ..persistence.repository import MemoryRepository
from ..delivery import SignalDelivery
from ..market.rest import BinanceClientImpl
from ..market.data import BinanceFuturesMarketData, configure_rest_concurrency
from ..delivery.telegram import build_message_broadcaster
from ..market.enrichment import PublicIntelligenceService
from ..diagnostics.quality import SignalQualityMonitor
from ..telemetry import TelemetryStore
from ..persistence.public_audit import PublicAuditLedger
from ..persistence.tracking import SignalTracker
from ..market.ws import FuturesWSManager

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


def build_application_container(
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

    client = market_data or BinanceFuturesMarketData(
        binance_client=BinanceClientImpl(
            rest_timeout_seconds=settings.ws.rest_timeout_seconds,
            futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
        )
    )

    bus = EventBus(
        max_size=settings.runtime.event_bus_max_size,
        warn_depth=settings.runtime.event_bus_warn_depth,
        drop_log_interval=settings.runtime.event_bus_drop_log_interval,
    )
    ws_manager: FuturesWSManager | None = None
    if settings.ws.enabled and isinstance(client, BinanceFuturesMarketData):
        ws_manager = FuturesWSManager(client, settings.ws)
        ws_manager.set_event_bus(bus)
        if hasattr(client, "_ws"):
            client._ws = ws_manager
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

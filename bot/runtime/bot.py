"""SignalBot — event-driven runtime using EventBus.

Architecture
------------
Primary path  : WS kline_close → EventBus.publish(KlineCloseEvent) → _on_kline_close
Fallback path : emergency scan every ``emergency_fallback_seconds`` if no kline events
Support tasks : shortlist refresh, OI refresh, heartbeat, health telemetry

``SignalPipeline`` is the only analysis entry point.  ``SignalBot`` orchestrates
market data, WebSocket subscriptions, shortlist management, signal selection,
delivery, and tracking.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import inspect
import logging
import os
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from bot.dashboard import BotDashboard
from bot.runtime.errors import DEFENSIVE_EXC
from bot.runtime.metrics import BotMetricsCollector

from ..delivery.confluence import ConfluenceEngine
from ..diagnostics.signals import SignalDiagnostics, set_global_diagnostics
from ..domain import (
    BookTickerEvent,
    BotSettings,
    KlineCloseEvent,
    PipelineResult,
    PreparedSymbol,
    ReconnectEvent,
    Signal,
    StrategyDecision,
    SymbolFrames,
    UniverseSymbol,
)
from ..feature_flags import FeatureFlags
from ..market.data import BinanceFuturesMarketData
from ..setups.base import SetupParams
from ..strategies import STRATEGY_CLASSES
from .container import build_application_container
from .cycle_runner import CycleRunner
from .delivery_orchestrator import DeliveryOrchestrator
from .fallback_runner import (
    FallbackRunner,
    run_emergency_fallback_loop,
    run_tracking_review_loop,
)
from .health_manager import (
    HealthManager,
    HealthMonitor,
    run_health_telemetry_loop,
    run_heartbeat_loop,
)
from .intra_candle_scanner import IntraCandleScanner
from .kline_handler import KlineHandler
from .market_context_updater import (
    MarketContextUpdater,
    run_market_regime_loop,
    run_public_intelligence_loop,
)
from .oi_refresh_runner import OIRefreshRunner, run_oi_refresh_loop
from .shortlist_service import ShortlistService, run_shortlist_refresh_loop
from .spot_refresh_runner import SpotRefreshRunner, run_spot_refresh_loop
from .symbol_analyzer import SymbolAnalyzer
from .telemetry_manager import TelemetryManager

if TYPE_CHECKING:
    from collections import Counter
    from datetime import datetime

    from ..engine import StrategyRegistry
    from ..persistence.tracking import SignalTrackingEvent
    from ..telemetry import TelemetryStore

LOG = logging.getLogger("bot.runtime.bot")


class SignalBot:
    """Event-driven signal bot runtime.

    Parameters
    ----------
    settings : BotSettings
        Runtime configuration.
    market_data : BinanceFuturesMarketData | None
        Market data client (created internally if ``None``).
    broadcaster : Any | None
        Telegram broadcaster (created internally if ``None``).
    telemetry : TelemetryStore | None
        Telemetry store (created internally if ``None``).
    """

    def __init__(
        self,
        settings: BotSettings,
        *,
        market_data: BinanceFuturesMarketData | None = None,
        broadcaster: Any | None = None,
        telemetry: TelemetryStore | None = None,
    ) -> None:
        self.settings = settings
        self.feature_flags = FeatureFlags(settings)
        container = build_application_container(
            settings,
            market_data=market_data,
            broadcaster=broadcaster,
            telemetry=telemetry,
            feature_flags=self.feature_flags,
            register_strategies=self._register_strategies_to_registry,
        )
        self.client = container.client
        self._bus = container.bus
        self._ws_manager = container.ws_manager
        self.telegram = container.telegram
        self.delivery = container.delivery
        self.public_audit = container.public_audit
        self.telemetry = container.telemetry
        self.alerts = container.alerts
        self._modern_repo = container.repository
        self.quality_monitor = container.quality_monitor
        self._signal_diagnostics = SignalDiagnostics()
        set_global_diagnostics(self._signal_diagnostics)
        LOG.info("MemoryRepository initialized | db=%s", self._modern_repo._db_path)

        # Note: All persistence now uses MemoryRepository (SQLite)
        # Legacy JSON stores (memory.json, state.json, tracking.json) removed in modern architecture

        self.confluence = ConfluenceEngine(settings, repository=self._modern_repo)

        from bot.regime.market import MarketRegimeAnalyzer

        self.market_regime = MarketRegimeAnalyzer(settings)

        self.metrics = BotMetricsCollector(
            settings.runtime.metrics_port, host=settings.runtime.metrics_host
        )
        disable_http = os.getenv("BOT_DISABLE_HTTP_SERVERS", "0").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        disable_dashboard = os.getenv("BOT_DISABLE_DASHBOARD", "0").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        enable_dashboard = os.getenv("BOT_ENABLE_DASHBOARD", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        self._metrics_enabled = not disable_http
        self._dashboard_enabled = enable_dashboard and not disable_dashboard
        # Back-compat for tests/callers that still flip this flag.
        self._http_servers_enabled = self._dashboard_enabled
        if not self._metrics_enabled:
            LOG.info("metrics server disabled via BOT_DISABLE_HTTP_SERVERS=1")
        else:
            self.metrics.start_server()
        if not self._dashboard_enabled:
            LOG.info(
                "dashboard disabled via BOT_DISABLE_DASHBOARD=1 or BOT_ENABLE_DASHBOARD=0"
            )

        self.dashboard = BotDashboard(
            self, settings.runtime.dashboard_port, host=settings.runtime.dashboard_host
        )

        # Tracking uses Modern MemoryRepository.
        # Legacy stores removed - all data in SQLite
        self.tracker = container.tracker
        self.intelligence = container.intelligence

        # Modern SignalEngine — core/ architecture (replaces legacy SignalPipeline)
        self._modern_registry = container.registry
        self._modern_engine = container.engine
        LOG.info("SignalEngine initialized with %d strategies", len(self._modern_registry))

        # Track fire-and-forget tasks for graceful shutdown
        self._background_tasks: set[asyncio.Task[Any]] = set()

        # Async state
        self._shutdown = asyncio.Event()
        self._analysis_semaphore = asyncio.Semaphore(settings.runtime.analysis_concurrency)
        self._last_kline_event_ts: float = 0.0
        self._shortlist: list[UniverseSymbol] = []
        self._last_live_shortlist: list[UniverseSymbol] = []
        self._last_shortlist_full_refresh_at: datetime | None = None
        self._symbol_meta_by_symbol: dict[str, Any] = {}
        self._shortlist_source: str = "startup"
        self._shortlist_lock = asyncio.Lock()
        self._cycle_failure_streak = 0
        self._circuit_open_until: float = 0.0
        self.last_cycle_summary: dict[str, Any] = {}
        self._session_action_delivered: int = 0
        self._zero_delivery_streak: int = 0
        self._last_zero_delivery_alert_mono: float = 0.0
        self._last_message_buffer_dropped: int = 0
        self._message_buffer_drop_baseline_set: bool = False
        self._last_message_buffer_drop_alert_mono: float = 0.0
        self._prepare_error_count: int = 0
        self._last_prepare_error: dict[str, Any] = {}
        self._diagnostic_trace_counts: dict[str, int] = {}
        self._running: bool = False
        self._research_harvest_service = None
        if settings.research_harvest.enabled:
            from .research_harvest_service import ResearchHarvestService

            self._research_harvest_service = ResearchHarvestService(self)
            LOG.info(
                "research_harvest_enabled | session=%s",
                self._research_harvest_service.recorder.session_dir,
            )

        # Intra-candle scan throttle — monotonic timestamp of last scan per symbol
        self._last_intra_scan: dict[str, float] = {}
        self._last_intra_mid: dict[str, float] = {}
        self._last_emergency_radar_scan: dict[str, float] = {}
        self._reconnect_refresh_task: asyncio.Task[None] | None = None
        self._shortlist_service = ShortlistService(self)
        self._cycle_runner = CycleRunner(self)
        self._symbol_analyzer = SymbolAnalyzer(self)
        self._delivery_orchestrator = DeliveryOrchestrator(self)
        self._health_manager = HealthManager(self)
        self._market_context_updater = MarketContextUpdater(self)
        self._intra_candle_scanner = IntraCandleScanner(self)
        self._kline_handler = KlineHandler(self)
        self._telemetry_manager = TelemetryManager(self)
        self._fallback_runner = FallbackRunner(self)
        self._oi_refresh_runner = OIRefreshRunner(self)
        self._spot_refresh_runner = SpotRefreshRunner(self)
        self._health_monitor = HealthMonitor(
            interval_seconds=float(self.settings.runtime.heartbeat_seconds),
            check=self.health_check,
            publish=lambda payload: self.telemetry.append_jsonl("health_runtime.jsonl", payload),
            alert=self._alert_critical,
            alert_after_failures=3,
        )

        # Subscribe to EventBus events
        self._bus.subscribe(KlineCloseEvent, self._on_kline_close)
        self._bus.subscribe(ReconnectEvent, self._on_reconnect)
        self._bus.subscribe(BookTickerEvent, self._on_book_ticker)
        LOG.info(
            "EventBus subscriptions registered | handlers=3 (kline_close, reconnect, book_ticker)"
        )
        if self._ws_manager is not None:
            self._ws_manager.register_agg_trade(self._on_ws_agg_trade)
            LOG.info("ws aggTrade callback registered for active-signal TP/SL fast-path")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def _noncritical_timeout_seconds(self) -> float:
        return max(self.settings.ws.rest_timeout_seconds * 2.0, 10.0)

    @property
    def _delivery_timeout_seconds(self) -> float:
        return max(self.settings.ws.rest_timeout_seconds * 8.0, 30.0)

    def _get_shortlist_service(self) -> ShortlistService:
        service = getattr(self, "_shortlist_service", None)
        if service is None:
            service = ShortlistService(self)
            self._shortlist_service = service
        return service

    def _get_symbol_analyzer(self) -> SymbolAnalyzer:
        analyzer = getattr(self, "_symbol_analyzer", None)
        if analyzer is None:
            analyzer = SymbolAnalyzer(self)
            self._symbol_analyzer = analyzer
        return analyzer

    def _get_delivery_orchestrator(self) -> DeliveryOrchestrator:
        orchestrator = getattr(self, "_delivery_orchestrator", None)
        if orchestrator is None:
            orchestrator = DeliveryOrchestrator(self)
            self._delivery_orchestrator = orchestrator
        return orchestrator

    def _get_cycle_runner(self) -> CycleRunner:
        runner = getattr(self, "_cycle_runner", None)
        if runner is None:
            runner = CycleRunner(self)
            self._cycle_runner = runner
        return runner

    def _get_intra_candle_scanner(self) -> IntraCandleScanner:
        scanner = getattr(self, "_intra_candle_scanner", None)
        if scanner is None:
            scanner = IntraCandleScanner(self)
            self._intra_candle_scanner = scanner
        return scanner

    def _get_telemetry_manager(self) -> TelemetryManager:
        manager = getattr(self, "_telemetry_manager", None)
        if manager is None:
            manager = TelemetryManager(self)
            self._telemetry_manager = manager
        return manager

    def _get_kline_handler(self) -> KlineHandler:
        handler = getattr(self, "_kline_handler", None)
        if handler is None:
            handler = KlineHandler(self)
            self._kline_handler = handler
        return handler

    def _get_fallback_runner(self) -> FallbackRunner:
        runner = getattr(self, "_fallback_runner", None)
        if runner is None:
            runner = FallbackRunner(self)
            self._fallback_runner = runner
        return runner

    def _get_oi_refresh_runner(self) -> OIRefreshRunner:
        runner = getattr(self, "_oi_refresh_runner", None)
        if runner is None:
            runner = OIRefreshRunner(self)
            self._oi_refresh_runner = runner
        return runner

    def _get_spot_refresh_runner(self) -> SpotRefreshRunner:
        runner = getattr(self, "_spot_refresh_runner", None)
        if runner is None:
            runner = SpotRefreshRunner(self)
            self._spot_refresh_runner = runner
        return runner

    def _spot_enrichments(self, symbol: str) -> dict[str, float]:
        return self._get_spot_refresh_runner().enrichments_for(symbol)

    def _decision_to_reject_row(self, *, symbol: str, decision: StrategyDecision) -> dict[str, Any]:
        return self._get_telemetry_manager().decision_to_reject_row(
            symbol=symbol, decision=decision
        )

    def _append_symbol_trace(self, *, symbol: str, row: dict[str, Any]) -> None:
        self._get_telemetry_manager().append_symbol_trace(symbol=symbol, row=row)

    def _append_strategy_decision_telemetry(
        self,
        *,
        symbol: str,
        trigger: str,
        decision: StrategyDecision,
    ) -> None:
        self._get_telemetry_manager().append_strategy_decision(
            symbol=symbol,
            trigger=trigger,
            decision=decision,
        )

    # ------------------------------------------------------------------
    # Modern Engine Migration
    # ------------------------------------------------------------------

    def _register_strategies_to_registry(self, registry: StrategyRegistry) -> None:
        """Register concrete strategies directly with a provided registry."""
        enabled_count = 0
        for strategy_cls in STRATEGY_CLASSES:
            setup_id = strategy_cls.setup_id
            is_enabled = bool(getattr(self.settings.setups, setup_id, False))
            strategy = strategy_cls(SetupParams(enabled=is_enabled), self.settings)
            registry.register(strategy, enabled=is_enabled)
            if is_enabled:
                enabled_count += 1
            LOG.info("registered strategy %s (enabled=%s)", setup_id, is_enabled)

        LOG.info(
            "strategies registered | total=%d enabled=%d",
            len(STRATEGY_CLASSES),
            enabled_count,
        )

    async def _run_modern_analysis(
        self,
        item: UniverseSymbol,
        frames: SymbolFrames,
        trigger: str = "modern_engine",
        event_ts: datetime | None = None,
        ws_enrichments: dict[str, Any] | None = None,
        kline_interval: str | None = None,
        max_setups: int | None = None,
        setup_subset: frozenset[str] | None = None,
    ) -> PipelineResult:
        return await self._symbol_analyzer.run_modern_analysis(
            item,
            frames,
            trigger=trigger,
            event_ts=event_ts,
            ws_enrichments=ws_enrichments,
            kline_interval=kline_interval,
            max_setups=max_setups,
            setup_subset=setup_subset,
        )

    def _select_and_rank(
        self,
        all_candidates: dict[str, list[Signal]],
        max_signals: int,
    ) -> list[Signal]:
        signals = self._delivery_orchestrator.select_and_rank(all_candidates, max_signals)
        min_score = float(self.settings.filters.min_score)
        return [signal for signal in signals if signal.score >= min_score]

    def _strategy_metadata(self, setup_id: str) -> Any | None:
        strategy = self._modern_registry.get(setup_id)
        return strategy.metadata if strategy is not None else None

    def _apply_strategy_metadata(
        self,
        signal: Signal,
        metadata: Any | None,
    ) -> Signal:
        if metadata is None:
            return signal
        return replace(
            signal,
            strategy_family=getattr(metadata, "family", signal.strategy_family),
            confirmation_profile=getattr(
                metadata, "confirmation_profile", signal.confirmation_profile
            ),
        )

    # Compatibility shims for existing tests/callers; logic lives in SymbolAnalyzer.
    def _directional_context(
        self,
        signal: Signal,
        prepared: PreparedSymbol,
    ) -> dict[str, Any]:
        return self._get_symbol_analyzer().directional_context(signal, prepared)

    def _check_family_precheck(
        self,
        signal: Signal,
        prepared: PreparedSymbol,
        metadata: Any | None,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        return self._get_symbol_analyzer().check_family_precheck(signal, prepared, metadata)

    def _apply_alignment_penalty(
        self,
        signal: Signal,
        prepared: PreparedSymbol,
        metadata: Any | None,
    ) -> tuple[Signal, dict[str, Any]]:
        return self._get_symbol_analyzer().apply_alignment_penalty(signal, prepared, metadata)

    def _check_family_confirmation(
        self,
        signal: Signal,
        prepared: PreparedSymbol,
        metadata: Any | None,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        return self._get_symbol_analyzer().check_family_confirmation(signal, prepared, metadata)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def _ensure_dashboard_started(self) -> None:
        """Start dashboard early so smoke/debug can inspect startup failures."""
        if not getattr(self, "_dashboard_enabled", False):
            return
        await self.dashboard.start_server_async(
            auto_open=self.settings.runtime.auto_open_dashboard
        )

    async def start(self) -> None:
        """Initial storage checks and WS bootstrap."""
        from pathlib import Path

        from ..market.proxy_bootstrap import ensure_network_ready

        updated = await ensure_network_ready(self.settings, config_path=Path("config.toml"))
        if updated is not self.settings:
            self.settings = updated
            LOG.info("network settings refreshed after bootstrap probe")

        self._preflight_storage_check()
        await self._preflight_delivery_check()

        # Initialize modern repository (SQLite)
        try:
            await self._modern_repo.initialize()
            LOG.info("modern repository initialized | SQLite ready")
        except DEFENSIVE_EXC as exc:
            msg = "modern repository init failed; runtime cannot track signals"
            raise RuntimeError(msg) from exc
        from ..diagnostics.config_audit import run_startup_audit

        run_startup_audit(self.settings)
        await self._ensure_dashboard_started()

        try:
            expired_count = await self._modern_repo.expire_open_signals_older_than(
                max_age_minutes=240
            )
            purged_cooldowns = await self._modern_repo.purge_cooldowns_older_than(
                max_age_minutes=120
            )
            if expired_count or purged_cooldowns:
                LOG.info(
                    "startup stale state cleanup | expired_open_signals=%d purged_cooldowns=%d",
                    expired_count,
                    purged_cooldowns,
                )
            self._startup_stale_expired = expired_count
        except DEFENSIVE_EXC:
            LOG.exception("startup stale state cleanup failed")

        repair_events: list[Any] = []
        try:
            repair_events = await self.tracker.repair_stuck_pending_activations(dry_run=False)
            repair_deliverable = [
                event
                for event in repair_events
                if getattr(event.tracked, "signal_message_id", None)
            ]
            if repair_deliverable:
                LOG.info(
                    "startup repaired stuck pending activations | events=%d deliverable=%d",
                    len(repair_events),
                    len(repair_deliverable),
                )
                await self._deliver_tracking(repair_deliverable)
            elif repair_events:
                LOG.info(
                    "startup repaired stuck pending activations | events=%d (no message_id edits)",
                    len(repair_events),
                )
        except DEFENSIVE_EXC:
            LOG.exception("startup stuck pending repair failed")

        startup_tracking_events: list[Any] = []
        try:
            startup_tracking_events = await self.tracker.review_open_signals(dry_run=False)
            if startup_tracking_events:
                LOG.info(
                    "startup tracking sweep closed open signals | events=%d",
                    len(startup_tracking_events),
                )
                await self._deliver_tracking(startup_tracking_events)
        except DEFENSIVE_EXC:
            LOG.exception("startup tracking sweep failed")

        try:
            rows = await self._modern_repo.get_active_signals()
            pending_n = sum(1 for row in rows if str(row.get("status") or "") == "pending")
            active_n = sum(1 for row in rows if str(row.get("status") or "") == "active")
            self._startup_tracking_summary = {
                "pending": pending_n,
                "active": active_n,
                "repaired": len(repair_events),
                "review_closed": len(startup_tracking_events),
                "stale_expired": int(getattr(self, "_startup_stale_expired", 0) or 0),
            }
        except DEFENSIVE_EXC:
            LOG.debug("startup tracking summary skipped", exc_info=True)
            self._startup_tracking_summary = None

        try:
            reconciled_outcomes = await self.tracker.reconcile_closed_outcomes()
            if reconciled_outcomes:
                LOG.info(
                    "startup reconciled closed signal outcomes | count=%d", reconciled_outcomes
                )
        except DEFENSIVE_EXC:
            LOG.exception("startup outcome reconciliation failed")

        # Get modern repository summary
        mem_summary = await self._modern_repo.summary()
        market_ctx = await self._modern_repo.get_market_context()
        await self._sync_ws_tracked_symbols()
        LOG.info(
            "runtime initialized | setups=%d shortlist_limit=%d "
            "memory_symbols=%d btc_bias=%s blacklisted=%s",
            len(self.settings.setups.enabled_setup_ids()),
            self.settings.universe.shortlist_limit,
            mem_summary.get("symbol_count", 0),
            market_ctx.get("btc_bias", "neutral"),
            mem_summary.get("blacklisted_symbols") or "not_blacklisted",
        )
        if self._ws_manager is not None:
            # Build full shortlist immediately instead of using only 4 pinned symbols
            try:
                shortlist_timeout_s = max(30.0, float(self.settings.ws.rest_timeout_seconds) * 2.5)
                shortlist = await asyncio.wait_for(
                    self._do_refresh_shortlist(),
                    timeout=shortlist_timeout_s,
                )
                symbols = [s.symbol for s in shortlist]
                LOG.info(
                    "starting ws_manager with shortlist | symbols=%d timeout=%.1fs",
                    len(symbols),
                    shortlist_timeout_s,
                )
            except TimeoutError:
                LOG.info(
                    "shortlist build timed out; using pinned | timeout=%.1fs pinned=%d",
                    shortlist_timeout_s,
                    len(self.settings.universe.pinned_symbols),
                )
                symbols = list(self.settings.universe.pinned_symbols)
            except DEFENSIVE_EXC:
                LOG.exception("shortlist build failed, using pinned fallback")
                symbols = list(self.settings.universe.pinned_symbols)

            try:
                await self._ws_manager.start(symbols)
            except DEFENSIVE_EXC:
                LOG.exception("ws_manager start failed; continuing with REST fallback")

        # Preload historical frames in the background so `prepare_symbol` can
        # meet its required 15m/1h history and optional 5m/4h context. This is deliberately
        # lightweight (batch + delay) to avoid REST storms.
        if isinstance(self.client, BinanceFuturesMarketData):
            preload_task = asyncio.create_task(
                self._preload_shortlist_frames(), name="preload_frames"
            )
            self._background_tasks.add(preload_task)
            def _preload_done(done: asyncio.Task[Any]) -> None:
                self._background_tasks.discard(done)
                SignalBot._log_background_task_failure(done)

            preload_task.add_done_callback(_preload_done)
        self._running = True
        self._log_autonomous_pipeline_armed()

    @staticmethod
    def _log_background_task_failure(task: asyncio.Task[Any]) -> None:
        # fix-20260604: Python 3.13+ silently drops unhandled task exceptions without this
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        if isinstance(exc, asyncio.CancelledError):
            return
        LOG.error(
            "background task failed | name=%s",
            task.get_name(),
            exc_info=exc,
        )

    def _log_autonomous_pipeline_armed(self) -> None:
        """Log background tasks started from run_forever (single entry: python main.py)."""
        radar_on = bool(getattr(self.settings.universe.radar, "enabled", False))
        intel_on = bool(self.intelligence is not None and self.settings.intelligence.enabled)
        LOG.info(
            "autonomous pipeline armed | entry=main.py event_bus=on "
            "shortlist_refresh=on heartbeat=on health_telemetry=on health_monitor=on "
            "emergency_fallback=on oi_refresh=on spot_companion=on tracking_review=on "
            "market_regime=on radar=%s intelligence=%s dashboard=%s metrics=%s",
            radar_on,
            intel_on,
            self._dashboard_enabled,
            self._metrics_enabled and self.metrics._enabled,
        )

    async def run_forever(self) -> None:
        """Main loop — EventBus-driven with emergency fallback."""
        bus_task = asyncio.create_task(self._bus.run(), name="event_bus")
        bus_task.add_done_callback(SignalBot._log_background_task_failure)
        # Give EventBus a moment to start before WS events arrive
        await asyncio.sleep(0.1)
        LOG.info("event bus started and ready")

        def _loop_task(coro: Any, *, name: str) -> asyncio.Task[None]:
            task = asyncio.create_task(coro, name=name)
            task.add_done_callback(SignalBot._log_background_task_failure)
            return task

        background_tasks: list[asyncio.Task[None]] = [
            _loop_task(run_shortlist_refresh_loop(self._shortlist_service), name="shortlist_refresh"),
            _loop_task(run_heartbeat_loop(self._health_manager), name="heartbeat"),
            _loop_task(run_health_telemetry_loop(self._health_manager), name="health_telemetry"),
            _loop_task(self._health_monitor.run(stop_event=self._shutdown), name="health_monitor"),
            _loop_task(run_emergency_fallback_loop(self._fallback_runner), name="emergency_fallback"),
            _loop_task(run_oi_refresh_loop(self._oi_refresh_runner), name="oi_refresh"),
            _loop_task(run_spot_refresh_loop(self._spot_refresh_runner), name="spot_companion"),
            _loop_task(run_tracking_review_loop(self._fallback_runner), name="tracking_review"),
            _loop_task(run_market_regime_loop(self._market_context_updater), name="market_regime"),
        ]
        if self.intelligence is not None and self.settings.intelligence.enabled:
            background_tasks.append(
                _loop_task(
                    run_public_intelligence_loop(self._market_context_updater),
                    name="public_intelligence",
                )
            )
        from .telegram_operator import TelegramOperatorConsole, operator_console_enabled

        if operator_console_enabled(self):
            console = TelegramOperatorConsole(self)
            self._operator_console = console
            background_tasks.append(
                _loop_task(
                    console.run_forever(stop_event=self._shutdown),
                    name="telegram_operator",
                )
            )
            LOG.info(
                "telegram operator console scheduled | operators=%s",
                list(self.settings.operator_user_ids),
            )

        LOG.info(
            "event-driven mode active | emergency_fallback=%ss",
            self.settings.runtime.emergency_fallback_seconds,
        )

        try:
            await self._shutdown.wait()
        finally:
            bus_task.cancel()
            for t in background_tasks:
                t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(bus_task, *background_tasks, return_exceptions=True)
            # close() is called by CLI finally block; don't duplicate here

    async def close(self) -> None:
        """Graceful shutdown."""
        self._running = False
        self._shutdown.set()

        harvest = getattr(self, "_research_harvest_service", None)
        if harvest is not None:
            try:
                harvest.finalize()
            except DEFENSIVE_EXC:
                LOG.debug("research_harvest finalize failed", exc_info=True)

        # Cancel and await fire-and-forget tasks. Drain the set until stable so
        # tasks spawned by cancellation callbacks cannot survive shutdown.
        while self._background_tasks:
            tasks = list(self._background_tasks)
            for task in tasks:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.difference_update(tasks)

        try:
            persist = getattr(self.tracker, "persist_tracking_state", None)
            if callable(persist):
                result = persist()
                if inspect.isawaitable(result):
                    await result
        except DEFENSIVE_EXC as exc:
            LOG.debug("tracker persist failed (non-fatal): %s", exc)

        if self._ws_manager is not None:
            await self._ws_manager.stop()
        if getattr(self, "_dashboard_enabled", False):
            await self.dashboard.stop_server_async()
        try:
            delivery_close = getattr(self.delivery, "close", None)
            if callable(delivery_close):
                result = delivery_close()
                if inspect.isawaitable(result):
                    await result
        except DEFENSIVE_EXC as exc:
            LOG.debug("delivery close failed (non-fatal): %s", exc)
        # Modern repository auto-closes with connection
        try:
            await self.alerts.close()
        except DEFENSIVE_EXC as exc:
            LOG.debug("alerts.close() failed (non-fatal): %s", exc)
        console = getattr(self, "_operator_console", None)
        if console is not None:
            try:
                await console.close()
            except DEFENSIVE_EXC as exc:
                LOG.debug("operator console close failed (non-fatal): %s", exc)

        # Close external resources (best-effort).
        try:
            await self._modern_repo.close()
        except DEFENSIVE_EXC as exc:
            LOG.debug("modern repo close failed (non-fatal): %s", exc)

        try:
            await self._get_spot_refresh_runner().close()
        except DEFENSIVE_EXC as exc:
            LOG.debug("spot companion close failed (non-fatal): %s", exc)

        try:
            close_md = getattr(self.client, "close", None)
            if callable(close_md):
                result = close_md()
                if inspect.isawaitable(result):
                    await result
        except DEFENSIVE_EXC as exc:
            LOG.debug("market data close failed (non-fatal): %s", exc)

        try:
            close_tg = getattr(self.telegram, "close", None)
            if callable(close_tg):
                result = close_tg()
                if inspect.isawaitable(result):
                    await result
        except DEFENSIVE_EXC as exc:
            LOG.debug("telegram close failed (non-fatal): %s", exc)

        try:
            from bot.delivery.ops_webhook import close_ops_webhook_session

            await close_ops_webhook_session(self)
        except DEFENSIVE_EXC as exc:
            LOG.debug("ops webhook session close failed (non-fatal): %s", exc)

        try:
            telemetry = getattr(self, "telemetry", None)
            run_id = getattr(telemetry, "run_id", None)
            finalize = getattr(telemetry, "finalize_run_metadata", None)
            if run_id and callable(finalize):
                collect = getattr(telemetry, "collect_session_totals", None)
                extras = {
                    "session_action_delivered": int(
                        getattr(self, "_session_action_delivered", 0) or 0
                    ),
                }
                diagnostics = getattr(self, "_signal_diagnostics", None)
                diag_summary = (
                    diagnostics.get_summary()
                    if diagnostics is not None and hasattr(diagnostics, "get_summary")
                    else {}
                )
                if isinstance(diag_summary, dict) and diag_summary:
                    extras["signal_diagnostics"] = diag_summary
                session_totals = collect(extras=extras) if callable(collect) else dict(extras)
                finalize(session_totals=session_totals)
        except DEFENSIVE_EXC as exc:
            LOG.debug("telemetry run_metadata finalize failed (non-fatal): %s", exc)

    async def health_check(self) -> dict[str, Any]:
        return await self._health_manager.health_check()

    # ------------------------------------------------------------------
    # EventBus handlers
    # ------------------------------------------------------------------

    async def _on_kline_close(self, event: KlineCloseEvent) -> None:
        """Delegate kline-close handling to KlineHandler."""
        await self._get_kline_handler().on_kline_close(event)

    async def _on_reconnect(self, event: ReconnectEvent) -> None:
        LOG.info("ws reconnected | reason=%s — scheduling shortlist resync", event.reason)
        self.metrics.record_ws_reconnect()
        prior = self._reconnect_refresh_task
        if prior is not None and not prior.done():
            prior.cancel()
        self._reconnect_refresh_task = asyncio.create_task(
            self._reconnect_shortlist_resync(event.reason),
            name="reconnect_shortlist_resync",
        )

    async def _reconnect_shortlist_resync(self, reason: str) -> None:
        """Debounced shortlist + WS tracked-symbol sync after WS reconnect."""
        try:
            await asyncio.sleep(2.0)
            if self._shutdown.is_set():
                return
            await self._do_refresh_shortlist()
            await self._sync_ws_tracked_symbols()
            LOG.info("ws reconnect shortlist resync complete | reason=%s", reason)
        except asyncio.CancelledError:
            raise
        except DEFENSIVE_EXC:
            LOG.exception("ws reconnect shortlist resync failed | reason=%s", reason)

    async def _on_book_ticker(self, event: BookTickerEvent) -> None:
        """Delegate intra-candle scan trigger handling to IntraCandleScanner."""
        await self._get_intra_candle_scanner().handle(event)

    # ------------------------------------------------------------------
    # Shared analysis logic — used by both kline_close and intra_candle paths
    # ------------------------------------------------------------------

    async def _select_and_deliver_for_symbol(
        self,
        symbol: str,
        result: PipelineResult,
    ) -> tuple[list[Signal], list[dict[str, Any]], list[Signal]]:
        return await self._get_kline_handler().select_and_deliver_for_symbol(symbol, result)

    # ------------------------------------------------------------------
    # Emergency fallback — full scan when no kline events
    # ------------------------------------------------------------------

    async def _run_emergency_cycle(self) -> dict[str, Any]:
        """Full shortlist analysis — used for emergency fallback."""
        return await self._get_cycle_runner().run_emergency_cycle()

    # ------------------------------------------------------------------
    # Frame fetching & enrichments
    # ------------------------------------------------------------------

    async def _fetch_frames(self, item: UniverseSymbol) -> SymbolFrames | None:
        return await self._symbol_analyzer.fetch_frames(item)

    async def _preload_shortlist_frames(self) -> None:
        await self._symbol_analyzer.preload_shortlist_frames()

    def _ws_cache_enrichments(self, symbol: str) -> dict[str, Any]:
        try:
            return self._symbol_analyzer.ws_cache_enrichments(symbol)
        except (AttributeError, KeyError, TypeError, ValueError):
            LOG.debug("ws_cache_enrichment_failed", extra={"symbol": symbol}, exc_info=True)
            return {}

    def _refresh_universe_symbol_from_ws(self, item: UniverseSymbol) -> UniverseSymbol:
        return self._symbol_analyzer.refresh_universe_symbol_from_ws(item)

    # ------------------------------------------------------------------
    # Background OI + L/S refresh
    # ------------------------------------------------------------------

    async def _apply_public_guardrails(self, snapshot: dict[str, Any]) -> None:
        await self._market_context_updater.apply_public_guardrails(snapshot)

    async def _update_memory_market_context(self, shortlist: list[UniverseSymbol]) -> None:
        await self._market_context_updater.update_memory_market_context(shortlist)

    def _compute_price_bias(self, symbol: str) -> str:
        return self._market_context_updater.compute_price_bias(symbol)

    # ------------------------------------------------------------------
    # Shortlist management
    # ------------------------------------------------------------------

    async def _fetch_symbols_with_retry(self, max_retries: int = 1) -> list[Any]:
        """Fetch exchange symbols with timeout and retry logic."""
        return await self._get_shortlist_service().fetch_symbols_with_retry(max_retries=max_retries)

    def _extract_symbol_assets(self, symbol: str) -> tuple[str | None, str | None]:
        return self._get_shortlist_service().extract_symbol_assets(symbol)

    def _build_pinned_shortlist(self) -> list[UniverseSymbol]:
        return self._get_shortlist_service().build_pinned_shortlist()

    async def _build_live_shortlist(
        self,
    ) -> tuple[list[UniverseSymbol], dict[str, Any]]:
        return await self._get_shortlist_service().build_live_shortlist()

    async def _sync_ws_tracked_symbols(self) -> None:
        if self._ws_manager is None:
            return
        try:
            from ..market.subscription_planner import merge_order_flow_tracked_symbols

            rows = await self._modern_repo.get_active_signals()
            pending_syms = sorted(
                {
                    str(row.get("symbol", "")).strip().upper()
                    for row in rows
                    if str(row.get("status") or "") == "pending"
                    and str(row.get("symbol", "")).strip()
                }
            )
            active_syms = sorted(
                {
                    str(row.get("symbol", "")).strip().upper()
                    for row in rows
                    if str(row.get("status") or "") == "active"
                    and str(row.get("symbol", "")).strip()
                }
            )
            shortlist_syms = [
                str(getattr(item, "symbol", "")).strip().upper()
                for item in self._shortlist
                if str(getattr(item, "symbol", "")).strip()
            ]
            priority_syms: list[str] = []
            ws_mgr = self._ws_manager
            radar_store = getattr(ws_mgr, "_radar_store", None) if ws_mgr is not None else None
            if radar_store is not None and getattr(self.settings.universe.radar, "enabled", False):
                from ..market.radar_state import SymbolTier

                priority_syms = [
                    s.upper()
                    for tier in (SymbolTier.HOT, SymbolTier.DEEP)
                    for s in radar_store.symbols_by_tier(tier)
                ]
            tracked_symbols = merge_order_flow_tracked_symbols(
                shortlist_syms,
                pending_symbols=pending_syms,
                active_symbols=active_syms,
                priority_symbols=priority_syms,
            )
            await self._ws_manager.set_tracked_symbols(tracked_symbols)
        except DEFENSIVE_EXC as exc:
            LOG.debug("tracked-symbol sync failed (non-fatal): %s", exc)

    async def _do_refresh_shortlist(self) -> list[UniverseSymbol]:
        return await self._get_shortlist_service().do_refresh_shortlist()

    async def _background_fetch_symbols(self) -> None:
        """Background task to fetch exchange symbols without blocking startup."""
        try:
            LOG.info("background fetch: attempting to get exchange symbols...")
            symbol_meta_list = await asyncio.wait_for(
                self.client.fetch_exchange_symbols(), timeout=30.0
            )
            self._symbol_meta_by_symbol = {
                str(getattr(row, "symbol", "")).strip().upper(): row for row in symbol_meta_list
            }
            LOG.info("background fetch: got %d exchange symbols", len(symbol_meta_list))
            # Could update shortlist here if needed, but pinned symbols are sufficient
        except DEFENSIVE_EXC as exc:
            LOG.debug("background fetch: failed to get exchange symbols: %s", exc)

    # ------------------------------------------------------------------
    # Delivery & tracking
    # ------------------------------------------------------------------

    async def _select_and_deliver(
        self,
        signals: list[Signal],
        *,
        prepared_by_tracking_id: dict[str, PreparedSymbol] | None = None,
    ) -> tuple[list[Signal], list[dict[str, Any]], Counter[str], int]:
        return await self._get_delivery_orchestrator().select_and_deliver(
            signals,
            prepared_by_tracking_id=prepared_by_tracking_id,
        )

    async def _close_superseded_signal(
        self, new_signal: Signal
    ) -> list[SignalTrackingEvent] | None:
        return await self._delivery_orchestrator.close_superseded_signal(new_signal)

    async def _on_ws_agg_trade(
        self,
        symbol: str,
        price: float,
        trade_dt: datetime,
    ) -> None:
        """Realtime limit fill + TP/SL on aggTrade ticks."""
        try:
            events = await self.tracker.on_agg_trade(symbol, price, trade_dt)
        except DEFENSIVE_EXC:
            LOG.exception("agg_trade tracking failed | symbol=%s", symbol)
            return
        if events:
            await self._deliver_tracking(events)

    async def _deliver_tracking(self, events: list[SignalTrackingEvent]) -> None:
        await self._delivery_orchestrator.deliver_tracking(events)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _preflight_storage_check(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.logs_dir.mkdir(parents=True, exist_ok=True)
        self.settings.telemetry_dir.mkdir(parents=True, exist_ok=True)
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def _preflight_delivery_check(self) -> None:
        try:
            await self.delivery.preflight_check()
            LOG.info("delivery preflight completed")
        except DEFENSIVE_EXC as exc:
            LOG.warning(
                "delivery preflight failed; continuing in signal-only/local mode: %s",
                exc,
            )

    async def _wait_noncritical(
        self, *, label: str, max_wait_s: float, operation: Any
    ) -> tuple[bool, Any | None]:
        try:
            result = await asyncio.wait_for(operation, timeout=max_wait_s)
        except TimeoutError:
            LOG.info(
                "%s timed out after %.1fs; skipping noncritical startup task", label, max_wait_s
            )
            return False, None
        except DEFENSIVE_EXC as exc:
            LOG.exception("%s failed; skipped noncritical startup task", label)
            await self._alert_critical(exc, {"label": label, "max_wait_s": max_wait_s})
            return False, None
        return True, result

    async def _alert_critical(self, exc: Exception, context: dict[str, Any]) -> None:
        from bot.delivery.telegram_routing import operator_dm_enabled, send_operator_html

        if not operator_dm_enabled(self, "send_critical_alerts"):
            return
        text = (
            "<b>🚨 CRITICAL ERROR</b>\n"
            f"<code>{html.escape(type(exc).__name__)}: {html.escape(str(exc))}</code>\n"
            f"<code>context={html.escape(str(context))}</code>\n"
            "<i>Operator alert · not sent to signal channel</i>"
        )
        try:
            await send_operator_html(self, text)
        except DEFENSIVE_EXC:
            LOG.debug("critical alert operator dispatch failed", exc_info=True)
        from bot.delivery.ops_webhook import send_ops_webhook_alert

        await send_ops_webhook_alert(
            self,
            event="critical_error",
            text=text,
            extra={"context": context, "exc": type(exc).__name__},
        )

    def _emit_telemetry_mismatch(
        self,
        *,
        symbol: str,
        trigger: str,
        mismatch_type: str,
        expected: dict[str, Any],
        actual: dict[str, Any],
    ) -> None:
        self._get_telemetry_manager().emit_telemetry_mismatch(
            symbol=symbol,
            trigger=trigger,
            mismatch_type=mismatch_type,
            expected=expected,
            actual=actual,
        )

    def _emit_cycle_log(
        self,
        *,
        symbol: str,
        interval: str,
        event_ts: datetime,
        shortlist_size: int,
        tracking_events: list[SignalTrackingEvent],
        result: PipelineResult,
        candidates: list[Signal],
        rejected: list[dict[str, Any]],
        delivered: list[Signal] | None = None,
    ) -> None:
        self._get_telemetry_manager().emit_cycle_log(
            symbol=symbol,
            interval=interval,
            event_ts=event_ts,
            shortlist_size=shortlist_size,
            tracking_events=tracking_events,
            result=result,
            candidates=candidates,
            rejected=rejected,
            delivered=delivered,
        )

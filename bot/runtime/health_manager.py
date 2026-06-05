from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.runtime.data_readiness import is_radar_promoted_item
from bot.market.proxy_bootstrap import network_probe_status
from bot.runtime.errors import DEFENSIVE_EXC

from ..features.prepare import cache_stats as frame_cache_stats

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bot.runtime.bot import SignalBot


LOG = logging.getLogger("bot.runtime.bot")


class HealthManager:
    def __init__(self, bot: SignalBot) -> None:
        self._bot = bot

    async def health_check(self) -> dict[str, Any]:
        ws_manager = self._bot._ws_manager
        ws_connected = bool(getattr(ws_manager, "is_connected", lambda: False)())
        ws_snapshot: dict[str, Any] = {}
        if ws_manager is not None and hasattr(ws_manager, "state_snapshot"):
            raw = ws_manager.state_snapshot()
            if isinstance(raw, dict):
                ws_snapshot = raw
        pending_outcomes = len(getattr(self._bot.tracker, "_pending_outcomes", []))
        try:
            active_rows = await self._bot._modern_repo.get_active_signals(include_closed=False)
            active_signals = len(
                [r for r in active_rows if r.get("status") in ("pending", "active")]
            )
        except DEFENSIVE_EXC:
            LOG.debug("health check active signal count failed", exc_info=True)
            active_signals = 0
        probe_flags = network_probe_status()
        radar_store = (
            getattr(ws_manager, "_radar_store", None) if ws_manager is not None else None
        )
        from bot.diagnostics.runtime_ops import assess_radar_store

        radar_health = assess_radar_store(
            radar_store,
            config=self._bot.settings.universe.radar,
        )
        return {
            "status": "healthy" if self._bot._running else "degraded",
            "radar": radar_health,
            "rest_probe_ok": probe_flags.get("rest_probe_ok"),
            "ws_probe_ok": probe_flags.get("ws_probe_ok"),
            "ws_connected": ws_connected,
            "fresh_tickers": int(ws_snapshot.get("fresh_tickers") or 0),
            "fresh_mark_prices": int(ws_snapshot.get("fresh_mark_prices") or 0),
            "order_flow_tracked_count": int(ws_snapshot.get("order_flow_tracked_count") or 0),
            "anchor_symbols_in_agg_trade": int(
                ws_snapshot.get("anchor_symbols_in_agg_trade") or 0
            ),
            "stale_kline_streams": int(ws_snapshot.get("stale_kline_streams") or 0),
            "active_signals": active_signals,
            "pending_outcomes": pending_outcomes,
            "shortlist_size": len(self._bot._shortlist),
            "last_kline_event_age_seconds": max(
                0.0,
                asyncio.get_running_loop().time() - self._bot._last_kline_event_ts,
            ),
            "frame_cache": frame_cache_stats(),
            "feature_flags": await self._bot.feature_flags.snapshot(),
        }

    async def heartbeat_periodic(self) -> None:
        while not self._bot._shutdown.is_set():
            await asyncio.sleep(300)
            if self._bot._shutdown.is_set():
                break
            async with self._bot._shortlist_lock:
                sl_size = len(self._bot._shortlist)
            active_sigs = await self._bot._modern_repo.get_active_signals()
            open_signals = len(active_sigs)
            ws_lag = 0.0
            ws_age = 0.0
            if self._bot._ws_manager is not None:
                ws_lag = self._bot._ws_manager._get_current_latency_ms() or 0
                ws_age = self._bot._ws_manager._last_message_age_seconds() or 0
            mem_summary = await self._bot._modern_repo.summary()
            blacklisted = mem_summary.get("blacklisted_symbols", [])
            market_ctx = await self._bot._modern_repo.get_market_context()

            regime_info = "n/a"
            if self._bot.market_regime._last_result is not None:
                r = self._bot.market_regime._last_result
                regime_info = f"{r.regime}:{r.strength:.1f}"

            LOG.info(
                "heartbeat | shortlist=%d open_signals=%d ws_lag_ms=%d ws_msg_age_s=%d "
                "market=%s btc_bias=%s memory_blacklist=%s",
                sl_size,
                open_signals,
                ws_lag,
                ws_age,
                regime_info,
                market_ctx.get("btc_bias", "neutral"),
                blacklisted or "not_blacklisted",
            )
            async with self._bot._shortlist_lock:
                shortlist = list(self._bot._shortlist)
            if shortlist:
                fit_counts = [len(item.strategy_fits) for item in shortlist]
                zero_fit = sum(1 for count in fit_counts if count == 0)
                avg_fit = sum(fit_counts) / len(fit_counts) if fit_counts else 0.0
                pinned_symbols = {
                    str(symbol).strip().upper()
                    for symbol in self._bot.settings.universe.pinned_symbols
                }
                pinned_count = sum(1 for item in shortlist if item.symbol.upper() in pinned_symbols)
                dynamic_count = len(shortlist) - pinned_count
                LOG.info(
                    "shortlist health | total=%d pinned=%d dynamic=%d "
                    "zero_strategy_fit=%d avg_strategy_fit=%.1f source=%s",
                    len(shortlist),
                    pinned_count,
                    dynamic_count,
                    zero_fit,
                    avg_fit,
                    self._bot._shortlist_source,
                )
                if zero_fit > len(shortlist) * 0.5:
                    LOG.warning(
                        "shortlist DEGRADED: >50%% symbols have zero strategy_fits "
                        "(%d/%d) - strategies will not run for these symbols",
                        zero_fit,
                        len(shortlist),
                    )
                if dynamic_count < 10:
                    LOG.warning(
                        "shortlist DEGRADED: only %d dynamic symbols (expect >= 30) "
                        "- check universe filter thresholds and WS-light fallback logic",
                        dynamic_count,
                    )
            diagnostics = getattr(self._bot, "_signal_diagnostics", None)
            if diagnostics is not None:
                diagnostics.log_summary(LOG)

            if self._bot.metrics._enabled:
                self._bot.metrics.update_bot_state(sl_size, open_signals, len(blacklisted))
                self._bot.metrics.record_ws_latency(ws_lag)
                self._bot.metrics.record_ws_message_age(ws_age)
                if self._bot._ws_manager is not None:
                    self._bot.metrics.update_ws_streams(len(self._bot._ws_manager._symbols))
                ws_mgr = self._bot._ws_manager
                radar_store = (
                    getattr(ws_mgr, "_radar_store", None) if ws_mgr is not None else None
                )
                radar_health = assess_radar_store(
                    radar_store,
                    config=self._bot.settings.universe.radar,
                )
                radar_promoted = sum(1 for item in shortlist if is_radar_promoted_item(item))
                self._bot.metrics.update_radar_metrics(
                    radar_health=radar_health,
                    radar_promoted_count=radar_promoted,
                )
                if self._bot.market_regime._last_result is not None:
                    r = self._bot.market_regime._last_result
                    self._bot.metrics.update_market_regime(
                        r.regime,
                        r.strength,
                        r.altcoin_season_index,
                    )

    async def health_telemetry_periodic(self) -> None:
        while not self._bot._shutdown.is_set():
            await asyncio.sleep(60)
            if self._bot._shutdown.is_set():
                break
            row: dict[str, Any] = {
                "ts": datetime.now(UTC).isoformat(),
                "prepare_error_count": self._bot._prepare_error_count,
                "frame_cache": frame_cache_stats(),
            }
            if self._bot._last_prepare_error:
                row["prepare_error_stage"] = self._bot._last_prepare_error.get("stage")
                row["prepare_error_exception_type"] = self._bot._last_prepare_error.get(
                    "exception_type"
                )
            ws_snapshot: dict[str, Any] = {}
            if self._bot._ws_manager is not None:
                raw_ws = self._bot._ws_manager.state_snapshot()
                if isinstance(raw_ws, dict):
                    ws_snapshot = raw_ws
                    row.update(ws_snapshot)
            from .delivery_alerts import check_message_buffer_drop_alert

            await check_message_buffer_drop_alert(
                self._bot,
                ws_snapshot=ws_snapshot,
            )
            rest_snapshot_func = getattr(self._bot.client, "state_snapshot", None)
            if callable(rest_snapshot_func):
                rest_snapshot = rest_snapshot_func()
                row.update(rest_snapshot if isinstance(rest_snapshot, dict) else {})
            ws_mgr = self._bot._ws_manager
            radar_store = getattr(ws_mgr, "_radar_store", None) if ws_mgr is not None else None
            row["radar"] = assess_radar_store(
                radar_store,
                config=self._bot.settings.universe.radar,
            )
            self._bot.telemetry.append_jsonl("health.jsonl", row)
            digest_runner = getattr(self._bot, "_operator_digest_runner", None)
            if digest_runner is None:
                from .operator_digest import OperatorDigestRunner

                digest_runner = OperatorDigestRunner(self._bot)
                self._bot._operator_digest_runner = digest_runner
            if int(datetime.now(UTC).timestamp()) % 1800 < 60:
                await digest_runner.maybe_send_digest(interval_seconds=1800.0)


class HealthMonitor:
    """Periodic health probe with repeated-failure alerting."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        check: Callable[[], Awaitable[dict[str, Any]]],
        publish: Callable[[dict[str, Any]], None] | None = None,
        alert: Callable[[Exception, dict[str, Any]], Awaitable[None]] | None = None,
        alert_after_failures: int = 3,
    ) -> None:
        self._interval_seconds = max(5.0, float(interval_seconds))
        self._check = check
        self._publish = publish
        self._alert = alert
        self._alert_after_failures = max(1, int(alert_after_failures))
        self._failure_streak = 0

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                payload = await self._check()
                payload["health_monitor_ts"] = datetime.now(UTC).isoformat()
                if self._publish is not None:
                    self._publish(payload)
                self._failure_streak = 0
            except asyncio.CancelledError:
                raise
            except DEFENSIVE_EXC as exc:
                self._failure_streak += 1
                LOG.exception(
                    "health monitor failure | streak=%d",
                    self._failure_streak,
                )
                if self._failure_streak >= self._alert_after_failures and self._alert is not None:
                    await self._alert(
                        exc,
                        {
                            "component": "health_monitor",
                            "failure_streak": self._failure_streak,
                        },
                    )
            await asyncio.sleep(self._interval_seconds)


async def run_heartbeat_loop(manager: HealthManager) -> None:
    """Background heartbeat log loop (started from SignalBot.run_forever)."""
    await manager.heartbeat_periodic()


async def run_health_telemetry_loop(manager: HealthManager) -> None:
    """Background health telemetry loop (started from SignalBot.run_forever)."""
    await manager.health_telemetry_periodic()

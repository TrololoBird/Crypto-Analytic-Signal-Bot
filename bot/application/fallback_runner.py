from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

LOG = logging.getLogger("bot.application.fallback_runner")


class FallbackRunner:
    """Owns periodic tracking review and emergency fallback scan loops."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    def _fallback_health_snapshot(self) -> dict[str, Any]:
        ws_message_age: float | None = None
        stale_stream_count = 0
        fresh_15m = 0
        tracked_symbols = 0
        if self._bot._ws_manager is not None:
            try:
                ws_message_age = self._bot._ws_manager._last_message_age_seconds()
                snapshot = self._bot._ws_manager.state_snapshot()
                stale_stream_count = int(snapshot.get("stale_kline_stream_count") or 0)
                fresh_15m = int(snapshot.get("fresh_klines_15m") or 0)
                tracked_symbols = int(snapshot.get("total_symbols") or snapshot.get("tracked_symbols") or 0)
            except Exception:
                pass
        bus_depth = 0
        bus_stats = getattr(self._bot._bus, "stats", None)
        if callable(bus_stats):
            try:
                bus_depth = int((bus_stats() or {}).get("current_depth") or 0)
            except Exception:
                bus_depth = 0
        analysis_permits = int(getattr(self._bot._analysis_semaphore, "_value", 0))
        return {
            "ws_message_age_seconds": ws_message_age,
            "stale_kline_stream_count": stale_stream_count,
            "fresh_klines_15m": fresh_15m,
            "tracked_symbols": tracked_symbols,
            "event_bus_depth": bus_depth,
            "analysis_permits": analysis_permits,
        }

    async def tracking_review_periodic(self) -> None:
        interval = 300  # 5 minutes
        while not self._bot._shutdown.is_set():
            await asyncio.sleep(interval)
            if self._bot._shutdown.is_set():
                break
            try:
                tracking_events = await self._bot.tracker.review_open_signals(dry_run=False)
                if tracking_events:
                    await self._bot._deliver_tracking(tracking_events)
            except Exception as exc:
                LOG.exception("tracking_review_periodic failed: %s", exc)

    async def emergency_fallback_scan(self) -> None:
        fallback_sec = self._bot.settings.runtime.emergency_fallback_seconds
        while not self._bot._shutdown.is_set():
            await asyncio.sleep(fallback_sec)
            if self._bot._shutdown.is_set():
                break

            time_since_event = asyncio.get_running_loop().time() - self._bot._last_kline_event_ts
            snapshot = self._fallback_health_snapshot()
            healthy_15m_flow = (
                snapshot["tracked_symbols"] > 0
                and snapshot["fresh_klines_15m"] >= max(1, int(snapshot["tracked_symbols"] * 0.7))
                and snapshot["stale_kline_stream_count"] == 0
            )
            ws_still_fresh = (
                snapshot["ws_message_age_seconds"] is not None
                and float(snapshot["ws_message_age_seconds"]) < float(fallback_sec)
            )
            has_backlog = snapshot["event_bus_depth"] > 0
            analysis_busy = snapshot["analysis_permits"] <= 0
            should_skip = (
                time_since_event < fallback_sec
                or (healthy_15m_flow and ws_still_fresh)
                or has_backlog
                or analysis_busy
            )

            if should_skip:
                self._bot.telemetry.append_jsonl(
                    "fallback_checks.jsonl",
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "trigger": "emergency_fallback",
                        "action": "skip",
                        "fallback_seconds": fallback_sec,
                        "time_since_last_kline_seconds": round(time_since_event, 1),
                        **snapshot,
                    },
                )
                continue

            self._bot.telemetry.append_jsonl(
                "fallback_checks.jsonl",
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "trigger": "emergency_fallback",
                    "action": "run",
                    "fallback_seconds": fallback_sec,
                    "time_since_last_kline_seconds": round(time_since_event, 1),
                    **snapshot,
                },
            )
            LOG.info(
                "emergency fallback: no kline events for %.0fs — running full scan",
                time_since_event,
            )
            try:
                await self._bot._run_emergency_cycle()
            except Exception as exc:
                LOG.warning("emergency fallback failed: %s", exc, exc_info=True)

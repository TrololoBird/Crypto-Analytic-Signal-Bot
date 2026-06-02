"""WebSocket broadcaster for live dashboard updates.

Subscribes to the bot's EventBus and pushes typed events
to all connected dashboard WebSocket clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.core.runtime_errors import DEFENSIVE_EXC

from ..domain.events import KlineCloseEvent, ReconnectEvent, ShortlistUpdatedEvent

if TYPE_CHECKING:
    from fastapi import WebSocket

    from ..core.event_bus import EventBus

from .live import funnel_stage_counts_from_cycle

LOG = logging.getLogger("bot.ws_dashboard")


def build_ws_health_payload(bot: Any) -> dict[str, Any]:
    """Build compact WS health payload for dashboard clients."""
    ws = getattr(bot, "_ws_manager", None)
    connected = False
    stale_klines = 0
    if ws is not None:
        is_connected = getattr(ws, "is_connected", None)
        if callable(is_connected):
            connected = bool(is_connected())
        snapshot_fn = getattr(ws, "state_snapshot", None)
        if callable(snapshot_fn):
            snapshot = snapshot_fn()
            if isinstance(snapshot, dict):
                stale_klines = int(snapshot.get("stale_kline_stream_count") or 0)
    return {
        "connected": connected,
        "stale_kline_stream_count": stale_klines,
        "ts": datetime.now(UTC).isoformat(),
    }


class DashboardWSBroadcaster:
    """Subscribes to EventBus and fans out typed JSON to dashboard WS clients."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._subscribed = False
        self._broadcast_tasks: set[asyncio.Task[None]] = set()

    def subscribe_to_bus(self) -> None:
        if self._subscribed:
            return
        try:
            self._bus.subscribe(KlineCloseEvent, self._on_kline_close)
            self._bus.subscribe(ShortlistUpdatedEvent, self._on_shortlist_updated)
            self._bus.subscribe(ReconnectEvent, self._on_reconnect)
            self._subscribed = True
            LOG.info("dashboard ws broadcaster subscribed to event bus events")
        except DEFENSIVE_EXC:
            LOG.exception("failed to subscribe ws broadcaster to event bus")

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        LOG.debug("dashboard ws client connected (%d total)", len(self._clients))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
        LOG.debug("dashboard ws client disconnected (%d remaining)", len(self._clients))

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            if not self._clients:
                return
            dead: list[WebSocket] = []
            msg = json.dumps(payload, default=str)
            for ws in self._clients:
                try:
                    await ws.send_text(msg)
                except DEFENSIVE_EXC:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)

    async def _on_kline_close(self, event: Any) -> None:
        await self.broadcast(
            {
                "type": "kline_close",
                "payload": {
                    "symbol": event.symbol,
                    "interval": event.interval,
                    "close_ts": event.close_ts,
                    "trigger": event.trigger,
                    "ts": datetime.now(UTC).isoformat(),
                },
            }
        )

    async def _on_shortlist_updated(self, event: Any) -> None:
        await self.broadcast(
            {
                "type": "shortlist_update",
                "payload": {
                    "symbols": list(event.symbols),
                    "ts": datetime.now(UTC).isoformat(),
                },
            }
        )

    async def _on_reconnect(self, event: Any) -> None:
        await self.broadcast(
            {
                "type": "reconnect",
                "payload": {"reason": event.reason, "ts": datetime.now(UTC).isoformat()},
            }
        )

    def publish_signal(self, signal: dict[str, Any]) -> None:
        """Push a live signal to all dashboard clients."""
        self._schedule_broadcast({"type": "signal", "payload": signal})

    def publish_regime(self, regime: dict[str, Any]) -> None:
        """Push a market regime update to all dashboard clients."""
        self._schedule_broadcast({"type": "regime_update", "payload": regime})

    def publish_funnel_update(self, payload: dict[str, Any]) -> None:
        """Push funnel stage counts to all dashboard clients."""
        self._schedule_broadcast({"type": "funnel_update", "payload": payload})

    def publish_ws_health(self, payload: dict[str, Any]) -> None:
        """Push runtime websocket health to all dashboard clients."""
        self._schedule_broadcast({"type": "ws_health", "payload": payload})

    def notify_cycle_complete(
        self,
        bot: Any,
        *,
        symbol: str,
        cycle_row: dict[str, Any],
        funnel: dict[str, Any] | None = None,
    ) -> None:
        """Broadcast funnel + WS health after a signal cycle completes."""
        stages = funnel_stage_counts_from_cycle(cycle_row=cycle_row, funnel=funnel)
        run_id = None
        telemetry = getattr(bot, "telemetry", None)
        if telemetry is not None:
            run_id = getattr(telemetry, "run_id", None)
        self.publish_funnel_update(
            {
                "ts": datetime.now(UTC).isoformat(),
                "symbol": symbol,
                "run_id": str(run_id) if run_id else None,
                "trigger": cycle_row.get("trigger"),
                "stages": stages,
            }
        )
        self.publish_ws_health(build_ws_health_payload(bot))

    def _schedule_broadcast(self, payload: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self.broadcast(payload))
            self._broadcast_tasks.add(task)
            task.add_done_callback(self._broadcast_tasks.discard)
        except RuntimeError:
            pass

    @property
    def client_count(self) -> int:
        return len(self._clients)

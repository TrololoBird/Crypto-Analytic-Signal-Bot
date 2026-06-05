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

from bot.runtime.errors import DEFENSIVE_EXC

from ..domain.events import KlineCloseEvent, ReconnectEvent, ShortlistUpdatedEvent

if TYPE_CHECKING:
    from fastapi import WebSocket

    from ..core.event_bus import EventBus

from .live import funnel_stage_counts_from_cycle

LOG = logging.getLogger("bot.ws_dashboard")

_WS_SEND_TIMEOUT_S = 3.0
_WS_QUEUE_MAXSIZE = 256
_PRIORITY_SIGNAL = 0
_PRIORITY_TRACKING = 1
_PRIORITY_FUNNEL = 2
_PRIORITY_SHORTLIST = 3
_PRIORITY_KLINE = 4
_PRIORITY_RECONNECT = 5


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
        self._queue: asyncio.Queue[tuple[int, dict[str, Any]]] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._dropped_count = 0

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
        await self._broadcast_with_timeout(payload)

    async def _broadcast_with_timeout(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            if not self._clients:
                return
            dead: list[WebSocket] = []
            msg = json.dumps(payload, default=str)
            for ws in self._clients:
                try:
                    await asyncio.wait_for(ws.send_text(msg), timeout=_WS_SEND_TIMEOUT_S)
                except DEFENSIVE_EXC:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)

    def _ensure_worker(self) -> None:
        worker_running = (
            self._queue is not None
            and self._worker_task is not None
            and not self._worker_task.done()
        )
        if worker_running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._queue = asyncio.Queue(maxsize=_WS_QUEUE_MAXSIZE)
        self._worker_task = loop.create_task(self._queue_worker(), name="dashboard-ws-broadcast")

    async def _queue_worker(self) -> None:
        queue = self._queue
        if queue is None:
            return
        while True:
            _priority, payload = await queue.get()
            try:
                await self._broadcast_with_timeout(payload)
            except DEFENSIVE_EXC:
                LOG.debug("dashboard ws broadcast worker error", exc_info=True)
            finally:
                queue.task_done()

    def _enqueue(self, payload: dict[str, Any], *, priority: int) -> None:
        self._ensure_worker()
        if self._queue is None:
            return
        try:
            self._queue.put_nowait((priority, payload))
        except asyncio.QueueFull:
            self._dropped_count += 1
            if self._dropped_count % 25 == 1:
                LOG.warning(
                    "dashboard ws broadcast queue full | dropped_total=%d type=%s",
                    self._dropped_count,
                    payload.get("type"),
                )

    async def _on_kline_close(self, event: Any) -> None:
        self._enqueue(
            {
                "type": "kline_close",
                "payload": {
                    "symbol": event.symbol,
                    "interval": event.interval,
                    "close_ts": event.close_ts,
                    "trigger": event.trigger,
                    "ts": datetime.now(UTC).isoformat(),
                },
            },
            priority=_PRIORITY_KLINE,
        )

    async def _on_shortlist_updated(self, event: Any) -> None:
        self._enqueue(
            {
                "type": "shortlist_update",
                "payload": {
                    "symbols": list(event.symbols),
                    "ts": datetime.now(UTC).isoformat(),
                },
            },
            priority=_PRIORITY_SHORTLIST,
        )

    async def _on_reconnect(self, event: Any) -> None:
        self._enqueue(
            {
                "type": "reconnect",
                "payload": {"reason": event.reason, "ts": datetime.now(UTC).isoformat()},
            },
            priority=_PRIORITY_RECONNECT,
        )

    def publish_tracking_update(self, payload: dict[str, Any]) -> None:
        """Notify dashboard clients that tracked signals changed."""
        body = dict(payload)
        body.setdefault("ts", datetime.now(UTC).isoformat())
        self._enqueue({"type": "tracking_update", "payload": body}, priority=_PRIORITY_TRACKING)

    def publish_signal(self, signal: dict[str, Any]) -> None:
        """Push a live signal to all dashboard clients."""
        self._enqueue({"type": "signal", "payload": signal}, priority=_PRIORITY_SIGNAL)

    def publish_regime(self, regime: dict[str, Any]) -> None:
        """Push a market regime update to all dashboard clients."""
        self._enqueue({"type": "regime_update", "payload": regime}, priority=_PRIORITY_FUNNEL)

    def publish_funnel_update(self, payload: dict[str, Any]) -> None:
        """Push funnel stage counts to all dashboard clients."""
        self._enqueue({"type": "funnel_update", "payload": payload}, priority=_PRIORITY_FUNNEL)

    def publish_ws_health(self, payload: dict[str, Any]) -> None:
        """Push runtime websocket health to all dashboard clients."""
        self._enqueue({"type": "ws_health", "payload": payload}, priority=_PRIORITY_FUNNEL)

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

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def client_count(self) -> int:
        return len(self._clients)

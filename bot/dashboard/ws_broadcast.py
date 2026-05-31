"""WebSocket broadcaster for live dashboard updates.

Subscribes to the bot's EventBus and pushes typed events
to all connected dashboard WebSocket clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket
    from ..core.event_bus import EventBus

LOG = logging.getLogger("bot.ws_dashboard")
UTC = timezone.utc


class DashboardWSBroadcaster:
    """Subscribes to EventBus and fans out typed JSON to dashboard WS clients."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._subscribed = False

    def subscribe_to_bus(self) -> None:
        if self._subscribed:
            return
        try:
            from ..domain.events import (
                KlineCloseEvent,
                ShortlistUpdatedEvent,
                ReconnectEvent,
            )

            self._bus.subscribe(KlineCloseEvent, self._on_kline_close)
            self._bus.subscribe(ShortlistUpdatedEvent, self._on_shortlist_updated)
            self._bus.subscribe(ReconnectEvent, self._on_reconnect)
            self._subscribed = True
            LOG.info("dashboard ws broadcaster subscribed to event bus events")
        except Exception:
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
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)

    async def _on_kline_close(self, event: Any) -> None:
        await self.broadcast({
            "type": "kline_close",
            "payload": {
                "symbol": event.symbol,
                "interval": event.interval,
                "close_ts": event.close_ts,
                "trigger": event.trigger,
                "ts": datetime.now(UTC).isoformat(),
            },
        })

    async def _on_shortlist_updated(self, event: Any) -> None:
        await self.broadcast({
            "type": "shortlist_update",
            "payload": {
                "symbols": list(event.symbols),
                "ts": datetime.now(UTC).isoformat(),
            },
        })

    async def _on_reconnect(self, event: Any) -> None:
        await self.broadcast({
            "type": "reconnect",
            "payload": {"reason": event.reason, "ts": datetime.now(UTC).isoformat()},
        })

    def publish_signal(self, signal: dict[str, Any]) -> None:
        """Push a live signal to all dashboard clients."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast({
                "type": "signal",
                "payload": signal,
            }))
        except RuntimeError:
            pass

    def publish_regime(self, regime: dict[str, Any]) -> None:
        """Push a market regime update to all dashboard clients."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast({
                "type": "regime_update",
                "payload": regime,
            }))
        except RuntimeError:
            pass

    @property
    def client_count(self) -> int:
        return len(self._clients)

"""Bounded in-memory EventBus with coalescing for hot-path events."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import defaultdict, deque
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from ..domain.events import (
    AnyEvent,
    BookTickerEvent,
    KlineCloseEvent,
    OIRefreshDueEvent,
    ShortlistUpdatedEvent,
)
from .runtime_errors import classify_runtime_error

LOG = logging.getLogger("bot.core.event_bus")

E = TypeVar("E", bound="AnyEvent")
AsyncHandler = Callable[["AnyEvent"], Coroutine[Any, Any, None]]


class EventBus:
    """Asyncio-native event bus with bounded backlog and typed coalescing."""

    def __init__(
        self,
        *,
        max_size: int = 512,
        warn_depth: int = 384,
        drop_log_interval: int = 25,
    ) -> None:
        self._max_size = max(1, int(max_size))
        self._warn_depth = max(1, int(min(warn_depth, self._max_size)))
        self._drop_log_interval = max(1, int(drop_log_interval))
        self._queue: deque[object] = deque()
        self._pending_events: dict[object, AnyEvent] = {}
        self._coalescing_keys: set[object] = set()
        self._ready = asyncio.Event()
        self._subscribers: dict[type, list[AsyncHandler]] = defaultdict(list)
        self._running = False
        self._lock = threading.RLock()
        self._sequence = 0
        self._high_water_mark = 0
        self._coalesced_count = 0
        self._dropped_count = 0
        self._handler_tasks: set[asyncio.Task[None]] = set()

    def subscribe(
        self,
        event_type: type[E],
        handler: Callable[[E], Coroutine[Any, Any, None]],
    ) -> None:
        self._subscribers[event_type].append(handler)  # type: ignore[arg-type]

    async def publish(self, event: AnyEvent) -> None:
        self.publish_nowait(event)

    def publish_nowait(self, event: AnyEvent) -> None:
        queue_depth: int
        dropped = False
        with self._lock:
            token, is_coalesced = self._event_token(event)
            if token in self._pending_events:
                self._pending_events[token] = event
                self._coalesced_count += 1
                queue_depth = len(self._queue)
            else:
                if len(self._queue) >= self._max_size:
                    dropped = not self._make_room_for(event)
                if not dropped:
                    if not is_coalesced:
                        token = ("queued", self._sequence)
                        self._sequence += 1
                    self._queue.append(token)
                    self._pending_events[token] = event
                    if is_coalesced:
                        self._coalescing_keys.add(token)
                    self._high_water_mark = max(self._high_water_mark, len(self._queue))
                queue_depth = len(self._queue)

        if dropped:
            self._record_drop(event)
            return

        if queue_depth >= self._warn_depth:
            LOG.error(
                "event bus backlog near capacity | depth=%d high_water=%d max_size=%d",
                queue_depth,
                self._high_water_mark,
                self._max_size,
            )
        self._ready.set()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "current_depth": len(self._queue),
                "high_water_mark": self._high_water_mark,
                "coalesced_count": self._coalesced_count,
                "dropped_count": self._dropped_count,
                "max_size": self._max_size,
            }

    async def run(self) -> None:
        self._running = True
        LOG.info(
            "event bus dispatch loop started | max_size=%d warn_depth=%d",
            self._max_size,
            self._warn_depth,
        )
        try:
            while True:
                token, event = self._pop_next_event()
                if event is None:
                    await self._wait_for_event()
                    continue

                event_type = type(event).__name__
                handlers = self._subscribers.get(type(event), [])
                LOG.debug(
                    "event bus dispatching | type=%s handlers=%d",
                    event_type,
                    len(handlers),
                )
                if not handlers:
                    LOG.debug("event bus no handlers for | type=%s", event_type)
                for handler in handlers:
                    task = asyncio.create_task(
                        self._safe_call(handler, event),
                        name=f"bus:{event_type}",
                    )
                    self._handler_tasks.add(task)
                    task.add_done_callback(self._task_done)
                    task.add_done_callback(self._handler_tasks.discard)

                with self._lock:
                    self._pending_events.pop(token, None)
                    self._coalescing_keys.discard(token)
        except asyncio.CancelledError:
            LOG.debug("event bus dispatch loop stopped")
            self._running = False
            for task in tuple(self._handler_tasks):
                task.cancel()
            if self._handler_tasks:
                await asyncio.gather(*self._handler_tasks, return_exceptions=True)
                self._handler_tasks.clear()

    def _pop_next_event(self) -> tuple[object | None, AnyEvent | None]:
        with self._lock:
            if not self._queue:
                return None, None
            token = self._queue.popleft()
            event = self._pending_events.pop(token, None)
            self._coalescing_keys.discard(token)
            return token, event

    async def _wait_for_event(self) -> None:
        with self._lock:
            if self._queue:
                return
            self._ready.clear()
        await self._ready.wait()

    def _event_token(self, event: AnyEvent) -> tuple[object, bool]:
        if isinstance(event, KlineCloseEvent):
            return ("kline_close", event.symbol, event.interval), True
        if isinstance(event, BookTickerEvent):
            return ("book_ticker", event.symbol), True
        if isinstance(event, ShortlistUpdatedEvent):
            return ("shortlist_updated",), True
        if isinstance(event, OIRefreshDueEvent):
            return ("oi_refresh_due",), True
        return ("unique", self._sequence), False

    def _make_room_for(self, event: AnyEvent) -> bool:
        event_name = type(event).__name__
        if isinstance(event, KlineCloseEvent):
            for index, token in enumerate(self._queue):
                queued = self._pending_events.get(token)
                if queued is not None and not isinstance(queued, KlineCloseEvent):
                    self._drop_queued_token(index, queued)
                    LOG.debug(
                        "event bus evicted non-critical %s to admit KlineCloseEvent",
                        type(queued).__name__,
                    )
                    return True
            for index, token in enumerate(self._queue):
                queued = self._pending_events.get(token)
                if isinstance(queued, KlineCloseEvent):
                    self._coalesce_queued_token(index, queued)
                    LOG.debug("event bus coalesced oldest KlineCloseEvent to admit newer close")
                    return True
            return False

        for index, token in enumerate(self._queue):
            queued = self._pending_events.get(token)
            if queued is not None and type(queued) is type(event):
                self._drop_queued_token(index, queued)
                LOG.debug("event bus evicted oldest %s to admit newer event", event_name)
                return True

        for index, token in enumerate(self._queue):
            queued = self._pending_events.get(token)
            if queued is not None and token in self._coalescing_keys:
                self._drop_queued_token(index, queued)
                LOG.debug(
                    "event bus evicted coalesced %s to admit newer %s",
                    type(queued).__name__,
                    event_name,
                )
                return True
        return False

    def _coalesce_queued_token(self, index: int, _event: AnyEvent) -> None:
        token = self._queue[index]
        del self._queue[index]
        self._pending_events.pop(token, None)
        self._coalescing_keys.discard(token)
        self._coalesced_count += 1

    def _drop_queued_token(self, index: int, event: AnyEvent) -> None:
        token = self._queue[index]
        del self._queue[index]
        self._pending_events.pop(token, None)
        self._coalescing_keys.discard(token)
        self._dropped_count += 1
        self._log_drop(event, reason="evicted")

    def _record_drop(self, event: AnyEvent) -> None:
        with self._lock:
            self._dropped_count += 1
        self._log_drop(event, reason="queue_full")

    def _log_drop(self, event: AnyEvent, *, reason: str) -> None:
        if self._dropped_count % self._drop_log_interval != 0:
            return
        LOG.error(
            "event bus dropping events | dropped=%d reason=%s last_type=%s stats=%s",
            self._dropped_count,
            reason,
            type(event).__name__,
            self.stats(),
        )

    @staticmethod
    async def _safe_call(handler: AsyncHandler, event: AnyEvent) -> None:
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.exception(
                "event_bus_handler_error",
                extra={
                    "handler": getattr(handler, "__qualname__", repr(handler)),
                    "event_type": type(event).__name__,
                    "exc": str(exc),
                    "error_class": classify_runtime_error(exc),
                },
            )

    @staticmethod
    def _task_done(task: asyncio.Task[None]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                LOG.error(
                    "unhandled exception in bus task %s: %s | error_class=%s",
                    task.get_name(),
                    exc,
                    classify_runtime_error(exc),
                )

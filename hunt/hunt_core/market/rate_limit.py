"""Client-side REST weight + request-window limiters for Binance public API.

Standalone hunt copy (hunt must not import ``engine.*`` — see ``_dev.check_imports``).
Binance USD-M REST is weight-budgeted at 2400/min per IP; we pace proactively well
under that. ``/futures/data/*`` has a separate request-count window handled by the
sliding-window limiter. ``WeightBudgetManager.force_floor`` syncs the local estimate
to the server-reported ``x-mbx-used-weight-1m`` header so pacing stays accurate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

LOG = logging.getLogger("hunt_core.market.rate_limit")

REST_WEIGHT_SOFT_LIMIT = 1800
REST_WEIGHT_PACE_LIMIT = 1500  # proactive cap - stay below Binance 2400/min with headroom
REST_WEIGHT_HARD_LIMIT = 2200
REST_WEIGHT_CRITICAL_LIMIT = 2350


class SlidingWindowRateLimiter:
    """Sliding-window limiter for request-count quotas (e.g. /futures/data/*)."""

    def __init__(self, *, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max(1, int(max_requests))
        self._window_seconds = max(1.0, float(window_seconds))
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()

    @property
    def max_requests(self) -> int:
        return self._max_requests

    async def acquire(self, *, label: str) -> float:
        waited_s = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._window_seconds
                while self._times and self._times[0] < cutoff:
                    self._times.popleft()
                if len(self._times) < self._max_requests:
                    self._times.append(now)
                    return waited_s
                sleep_s = max(0.0, (self._times[0] + self._window_seconds) - now) + 0.05
                log_fn = LOG.debug if sleep_s < 2.0 else LOG.info
                log_fn(
                    "futures-data request pacing | sleeping=%.2fs label=%s used=%d limit=%d window=%.0fs",
                    sleep_s,
                    label,
                    len(self._times),
                    self._max_requests,
                    self._window_seconds,
                )
            await asyncio.sleep(sleep_s)
            waited_s += sleep_s


class WeightBudgetManager:
    """Client-side request-weight queue for Binance public REST calls."""

    def __init__(self, *, max_weight: int, window_seconds: float) -> None:
        self._max_weight = max(1, int(max_weight))
        self._window_seconds = max(1.0, float(window_seconds))
        self._events: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    @property
    def used_weight(self) -> int:
        now = time.monotonic()
        cutoff = now - self._window_seconds
        return sum(weight for ts, weight in self._events if ts >= cutoff)

    def force_floor(self, server_weight: int) -> None:
        """Sync the local estimate to at least the server-reported used weight.

        Binance returns ``x-mbx-used-weight-1m`` on every response; if the server
        reports more than we tracked (cached endpoints, ccxt.pro snapshots), inject
        the gap so the next ``acquire`` paces correctly. Called from the event-loop
        thread (no lock needed).
        """
        normalized = max(0, int(server_weight))
        if normalized <= 0:
            return
        now = time.monotonic()
        cutoff = now - self._window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        current = sum(w for _, w in self._events)
        gap = normalized - current
        if gap > 0:
            self._events.append((now, gap))
            if gap > 50:
                LOG.info("weight floor sync | server=%d local=%d gap=+%d", normalized, current, gap)

    async def acquire(self, *, weight: int, label: str) -> float:
        normalized_weight = max(0, int(weight))
        if normalized_weight <= 0:
            return 0.0
        waited_s = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._window_seconds
                while self._events and self._events[0][0] < cutoff:
                    self._events.popleft()
                used = sum(item_weight for _ts, item_weight in self._events)
                if used + normalized_weight <= self._max_weight:
                    self._events.append((now, normalized_weight))
                    return waited_s
                oldest_ts = self._events[0][0] if self._events else now
                sleep_s = max(0.0, (oldest_ts + self._window_seconds) - now) + 0.05
                log_fn = LOG.debug if sleep_s < 2.0 else LOG.info
                log_fn(
                    "REST weight pacing | sleeping=%.2fs label=%s used=%d requested=%d pace_limit=%d window=%.0fs",
                    sleep_s,
                    label,
                    used,
                    normalized_weight,
                    self._max_weight,
                    self._window_seconds,
                )
            await asyncio.sleep(sleep_s)
            waited_s += sleep_s

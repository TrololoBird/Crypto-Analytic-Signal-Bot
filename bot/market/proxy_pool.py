"""Rotating egress proxy pool with exponential-backoff and circuit breaker."""

from __future__ import annotations

__all__ = [
    "BanDetectionPolicy",
    "ProxyPool",
    "is_proxy_transport_error",
]

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from bot.market.network_proxy import mask_proxy_url

LOG = logging.getLogger("bot.market.proxy_pool")

_CIRCUIT_BREAKER_MAX_FAILURES = 5  # consecutive failures → permanent removal
_MAX_BACKOFF_SECONDS = 3600.0  # 1h cap on per-proxy exponential backoff


@dataclass
class BanDetectionPolicy:
    """Configuration for what constitutes a proxy ban — mirrors scrapy-rotating-proxies pattern.

    Inspectors call the methods below to decide whether a response or
    exception indicates the proxy was banned by the target.
    """

    ban_status_codes: frozenset[int] = frozenset({418, 403, 429})
    ban_on_timeout: bool = True
    ban_on_proxy_error: bool = True

    def is_response_banned(self, status: int) -> bool:
        return status in self.ban_status_codes

    def is_exception_banned(self, exc: BaseException) -> bool:
        if not (self.ban_on_proxy_error or self.ban_on_timeout):
            return False
        if self.ban_on_proxy_error and is_proxy_transport_error(exc):
            return True
        if self.ban_on_timeout:
            if isinstance(exc, TimeoutError):
                return True
            if exc.__class__.__name__ == "TimeoutError":
                return True
        return False


@dataclass
class ProxyPool:
    """Round-robin proxy list with per-proxy exponential backoff on failure.

    Base cooldown (``cooldown_seconds``, default 300 s) is doubled on each
    consecutive failure (1 -> 300 s, 2 -> 600 s, 3 -> 1200 s, ...) capped at 1 h.
    A single intervening ``mark_success`` resets the backoff to the base value.

    Circuit breaker: after ``_CIRCUIT_BREAKER_MAX_FAILURES`` consecutive failures
    without an intervening success, the URL is removed from the pool permanently.
    """

    urls: list[str]
    cooldown_seconds: float = 300.0
    _index: int = 0
    _bad_until: dict[str, float] = field(default_factory=dict)
    _success_count: dict[str, int] = field(default_factory=dict)
    _failure_count: dict[str, int] = field(default_factory=dict)
    _success_streak: dict[str, int] = field(default_factory=dict)
    _last_latencies: dict[str, list[float]] = field(default_factory=dict)
    _rolling_window: int = 10
    _failover_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def from_urls(
        cls,
        urls: list[str],
        *,
        cooldown_seconds: float = 300.0,
    ) -> ProxyPool | None:
        cleaned = _dedupe_urls(urls)
        if not cleaned:
            return None
        return cls(cleaned, cooldown_seconds=max(30.0, float(cooldown_seconds or 120.0)))

    def has_alternatives(self) -> bool:
        return len(self.urls) > 1

    def current(self) -> str:
        return self.urls[self._index % len(self.urls)]

    def _is_available(self, url: str) -> bool:
        return time.monotonic() >= self._bad_until.get(url, 0.0)

    def _next_available_index(self, _start: int) -> int | None:
        available = [
            (i, self.urls[i])
            for i in range(len(self.urls))
            if self._is_available(self.urls[i])
        ]
        if not available:
            return None
        if len(available) == 1:
            return available[0][0]
        weights = [self._health_score(url) for _, url in available]
        total = sum(weights)
        if total <= 0:
            return available[0][0]
        chosen = random.choices(range(len(available)), weights=weights, k=1)[0]
        return available[chosen][0]

    def _backoff_duration(self, failures: int) -> float:
        """Exponential backoff + 10 % jitter, capped at ``_MAX_BACKOFF_SECONDS``."""
        base = min(_MAX_BACKOFF_SECONDS, self.cooldown_seconds * (2 ** (failures - 1)))
        return base + random.uniform(0, base * 0.1)  # type: ignore[no-any-return]

    def mark_failed(self, url: str, reason: str) -> str | None:
        """Mark *url* bad with exponential backoff; circuit-breaker on N consecutive."""
        if url in self.urls:
            self._failure_count[url] = self._failure_count.get(url, 0) + 1
            self._success_streak[url] = 0  # reset streak on failure
            backoff = self._backoff_duration(self._failure_count[url])
            self._bad_until[url] = time.monotonic() + backoff
            LOG.warning(
                "proxy marked bad | url=%s backoff_s=%.0f failures=%d reason=%s",
                mask_proxy_url(url),
                backoff,
                self._failure_count[url],
                reason[:120],
            )
            # Circuit breaker: permanent removal after N consecutive failures
            if self._failure_count[url] >= _CIRCUIT_BREAKER_MAX_FAILURES:
                self._remove_url(url, f"circuit_breaker:{_CIRCUIT_BREAKER_MAX_FAILURES}_failures")
        start = (self.urls.index(url) + 1) if url in self.urls else self._index + 1
        nxt = self._next_available_index(start)
        if nxt is None:
            LOG.error("proxy pool exhausted | all endpoints in cooldown")
            return None
        self._index = nxt
        active = self.current()
        LOG.info("proxy failover active | url=%s", mask_proxy_url(active))
        return active

    def _remove_url(self, url: str, reason: str) -> None:
        LOG.warning(
            "proxy removed | url=%s reason=%s pool_size=%d",
            mask_proxy_url(url),
            reason,
            len(self.urls) - 1,
        )
        if url in self.urls:
            self.urls = [u for u in self.urls if u != url]
        self._bad_until.pop(url, None)
        self._success_count.pop(url, None)
        self._failure_count.pop(url, None)
        self._success_streak.pop(url, None)
        self._last_latencies.pop(url, None)
        if self._index >= len(self.urls) and self.urls:
            self._index = self._index % len(self.urls)

    def mark_success(self, url: str, latency_ms: float | None = None) -> None:
        """Record a successful request; resets failure count for circuit breaker.

        If *latency_ms* is provided, stores it in a rolling window for health scoring.
        """
        if url in self.urls:
            self._success_count[url] = self._success_count.get(url, 0) + 1
            self._failure_count[url] = 0  # reset circuit breaker on success
            self._success_streak[url] = self._success_streak.get(url, 0) + 1
            if latency_ms is not None:
                samples = self._last_latencies.setdefault(url, [])
                samples.append(latency_ms)
                if len(samples) > self._rolling_window:
                    samples.pop(0)

    def _health_score(self, url: str) -> float:
        success = float(self._success_count.get(url, 0))
        failure = float(self._failure_count.get(url, 0))
        total = success + failure
        if total <= 0.0:
            return 1.0 if self._is_available(url) else 0.0
        base = success / total
        if not self._is_available(url):
            return base * 0.25
        # Streak bonus: +0.1 per consecutive success (cap +0.3)
        streak = float(self._success_streak.get(url, 0))
        streak_bonus = min(0.3, streak * 0.1)
        # Latency bonus: 0.0-0.2 based on average latency (lower = better)
        lat_samples = self._last_latencies.get(url, [])
        if lat_samples:
            avg_lat = sum(lat_samples) / len(lat_samples)
            if avg_lat < 100:
                latency_bonus = 0.2
            elif avg_lat < 300:
                latency_bonus = 0.15
            elif avg_lat < 800:
                latency_bonus = 0.1
            elif avg_lat < 2000:
                latency_bonus = 0.05
            else:
                latency_bonus = 0.0
        else:
            latency_bonus = 0.0
        return min(1.0, base + streak_bonus + latency_bonus)

    def reset(self, urls: list[str]) -> None:
        """Replace pool URLs and reset all health state."""
        cleaned = _dedupe_urls(urls)
        if not cleaned:
            return
        self.urls = cleaned
        self._bad_until.clear()
        self._success_count.clear()
        self._failure_count.clear()
        self._success_streak.clear()
        self._last_latencies.clear()
        self._index = 0

    async def revalidate(
        self,
        validate_fn: Callable[[str, asyncio.Semaphore, float], tuple[str, float] | None],
        *,
        concurrency: int = 20,
        timeout_s: float = 10.0,
    ) -> int:
        """Re-test all pool URLs with *validate_fn*; remove dead ones.

        ``validate_fn(url)`` should return ``(url, latency_ms)`` on success
        and ``None`` on failure.  Returns number of URLs removed.
        """
        if not self.urls:
            return 0
        sem = asyncio.Semaphore(min(concurrency, len(self.urls)))
        results = await asyncio.gather(
            *[validate_fn(u, sem, timeout_s) for u in self.urls],
            return_exceptions=True,
        )
        dead: list[str] = []
        for url, result in zip(self.urls, results, strict=False):
            if not isinstance(result, tuple):
                dead.append(url)
        for url in dead:
            self._remove_url(url, "revalidate_failed")
        return len(dead)

    def rotate_after_failure(self, failed_url: str | None, reason: str) -> str | None:
        if not self.has_alternatives():
            return None
        return self.mark_failed(failed_url or self.current(), reason)

    def log_metrics(self, context: str = "") -> None:
        snap = self.snapshot()
        LOG.info(
            "proxy_pool_metrics | context=%s active=%s count=%d cooldown=%d "
            "health_avg=%.3f failover_locked=%s%s",
            context,
            snap["active"],
            snap["count"],
            snap["in_cooldown"],
            cast("float", snap["health_score_avg"]),
            self._failover_lock.locked(),
            f" {snap.get('session_manager', '')}" if snap.get("session_manager") else "",
        )

    def snapshot(self) -> dict[str, object]:
        now = time.monotonic()
        return {
            "active": mask_proxy_url(self.current()),
            "count": len(self.urls),
            "in_cooldown": sum(1 for url in self.urls if not self._is_available(url)),
            "cooldown_seconds": self.cooldown_seconds,
            "endpoints": [
                {
                    "url": mask_proxy_url(url),
                    "available": self._is_available(url),
                    "cooldown_remaining_s": max(0.0, self._bad_until.get(url, 0.0) - now),
                    "success_count": self._success_count.get(url, 0),
                    "failure_count": self._failure_count.get(url, 0),
                    "success_streak": self._success_streak.get(url, 0),
                    "health_score": round(self._health_score(url), 4),
                    "latency_avg_ms": round(
                        sum(self._last_latencies.get(url, []))
                        / max(len(self._last_latencies.get(url, [])), 1)
                    ) if self._last_latencies.get(url) else None,
                }
                for url in self.urls
            ],
            "health_score_avg": round(
                sum(self._health_score(url) for url in self.urls) / max(len(self.urls), 1),
                4,
            ),
        }


def _dedupe_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    for raw in urls:
        value = str(raw or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def is_proxy_transport_error(exc: BaseException) -> bool:
    """True when failure likely indicates blocked or dead proxy transport."""
    name = exc.__class__.__name__
    if name in {"ProxyConnectionError", "ProxyTimeoutError", "ProxyError"}:
        return True
    message = str(exc).lower()
    markers = (
        "couldn't connect to proxy",
        "proxy connection refused",
        "proxy timed out",
        "name or service not known",
        "getaddrinfo failed",
    )
    return any(marker in message for marker in markers)

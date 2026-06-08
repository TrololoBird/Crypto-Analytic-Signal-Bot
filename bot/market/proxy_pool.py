"""Rotating egress proxy pool with exponential-backoff and circuit breaker."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from bot.market.network_proxy import mask_proxy_url

LOG = logging.getLogger("bot.market.proxy_pool")

_CIRCUIT_BREAKER_MAX_FAILURES = 5  # consecutive failures → permanent removal
_MAX_BACKOFF_SECONDS = 3600.0  # 1h cap on per-proxy exponential backoff


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

    def _next_available_index(self, start: int) -> int | None:
        count = len(self.urls)
        for offset in range(count):
            idx = (start + offset) % count
            if self._is_available(self.urls[idx]):
                return idx
        return None

    def _backoff_duration(self, failures: int) -> float:
        """Exponential backoff capped at ``_MAX_BACKOFF_SECONDS``."""
        return min(_MAX_BACKOFF_SECONDS, self.cooldown_seconds * (2 ** (failures - 1)))  # type: ignore[no-any-return]

    def mark_failed(self, url: str, reason: str) -> str | None:
        """Mark *url* bad with exponential backoff; circuit-breaker on N consecutive."""
        if url in self.urls:
            self._failure_count[url] = self._failure_count.get(url, 0) + 1
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
        if self._index >= len(self.urls) and self.urls:
            self._index = self._index % len(self.urls)

    def mark_success(self, url: str) -> None:
        """Record a successful request; resets failure count for circuit breaker."""
        if url in self.urls:
            self._success_count[url] = self._success_count.get(url, 0) + 1
            self._failure_count[url] = 0  # reset circuit breaker on success

    def _health_score(self, url: str) -> float:
        success = float(self._success_count.get(url, 0))
        failure = float(self._failure_count.get(url, 0))
        total = success + failure
        if total <= 0.0:
            return 1.0 if self._is_available(url) else 0.0
        base = success / total
        if not self._is_available(url):
            return base * 0.25
        return base

    def reset(self, urls: list[str]) -> None:
        """Replace pool URLs and reset all health state."""
        cleaned = _dedupe_urls(urls)
        if not cleaned:
            return
        self.urls = cleaned
        self._bad_until.clear()
        self._success_count.clear()
        self._failure_count.clear()
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
                    "health_score": round(self._health_score(url), 4),
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

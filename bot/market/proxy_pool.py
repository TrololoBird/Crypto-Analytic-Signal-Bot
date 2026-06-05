"""Rotating egress proxy pool with cooldown-based failover."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from bot.market.network_proxy import mask_proxy_url

LOG = logging.getLogger("bot.market.proxy_pool")


@dataclass
class ProxyPool:
    """Round-robin proxy list; failed endpoints cool off then become eligible again."""

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
        return cls(cleaned, cooldown_seconds=max(30.0, float(cooldown_seconds)))

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

    def mark_failed(self, url: str, reason: str) -> str | None:
        """Mark *url* bad and advance to the next available proxy."""
        if url in self.urls:
            self._failure_count[url] = self._failure_count.get(url, 0) + 1
            self._bad_until[url] = time.monotonic() + self.cooldown_seconds
            LOG.warning(
                "proxy marked bad | url=%s cooldown_s=%.0f reason=%s",
                mask_proxy_url(url),
                self.cooldown_seconds,
                reason[:120],
            )
        start = (self.urls.index(url) + 1) if url in self.urls else self._index + 1
        nxt = self._next_available_index(start)
        if nxt is None:
            LOG.error("proxy pool exhausted | all endpoints in cooldown")
            return None
        self._index = nxt
        active = self.current()
        LOG.info("proxy failover active | url=%s", mask_proxy_url(active))
        return active

    def mark_success(self, url: str) -> None:
        """Record a successful request through *url* for health scoring."""
        if url in self.urls:
            self._success_count[url] = self._success_count.get(url, 0) + 1

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
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, OSError)):
        return True
    message = str(exc).lower()
    markers = (
        "proxy",
        "couldn't connect to proxy",
        "connection refused",
        "timed out",
        "name or service not known",
        "getaddrinfo failed",
    )
    return any(marker in message for marker in markers)

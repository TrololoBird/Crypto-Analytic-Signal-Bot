"""Per-proxy session manager with automatic rotation on failover."""

from __future__ import annotations

__all__ = [
    "ProxySessionManager",
    "get_per_proxy_concurrency",
    "set_per_proxy_concurrency",
]

import asyncio
import logging
from typing import TYPE_CHECKING

from bot.market.network_proxy import close_aiohttp_session, mask_proxy_url

# Module-level per-proxy concurrency — set via configure_rest_concurrency() at startup.
_PER_PROXY_CONCURRENCY: int = 3


def set_per_proxy_concurrency(n: int) -> None:
    global _PER_PROXY_CONCURRENCY  # noqa: PLW0603
    _PER_PROXY_CONCURRENCY = max(1, n)


def get_per_proxy_concurrency() -> int:
    return _PER_PROXY_CONCURRENCY


if TYPE_CHECKING:
    from collections.abc import Callable

    import aiohttp

    from bot.market.proxy_pool import ProxyPool

LOG = logging.getLogger("bot.market._proxy_session")


class ProxySessionManager:
    """Manages per-proxy aiohttp sessions; creates on first use, evicts on failover.

    Session key is ``str | None`` — ``None`` represents direct (no proxy) egress.
    On pool rotation the old proxy's session is closed and removed.
    On pool refresh, sessions for URLs that are no longer in the pool are closed.
    """

    def __init__(
        self,
        pool: ProxyPool | None,
        session_factory: Callable[[str | None], aiohttp.ClientSession],
        *,
        active_proxy_url: str | None = None,
    ) -> None:
        self._pool = pool
        self._factory = session_factory
        self._sessions: dict[str | None, aiohttp.ClientSession] = {}
        self._active_key: str | None = active_proxy_url
        self._semaphores: dict[str | None, asyncio.Semaphore] = {}
        self._per_proxy_concurrency: int = _PER_PROXY_CONCURRENCY

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def active_key(self) -> str | None:
        return self._active_key

    @property
    def pool(self) -> ProxyPool | None:
        return self._pool

    def current(self) -> str | None:
        """Return the active proxy URL (or ``None`` for direct).

        Always returns the value last set via ``set_active``, ``rotate``,
        or ``refresh_pool`` — never reads from the pool directly.
        """
        return self._active_key

    def get_semaphore(self) -> asyncio.Semaphore:
        """Return (or create) the per-proxy concurrency semaphore for the active proxy.

        Replaces the process-wide ``rest_global_semaphore()`` with a per-proxy
        gate so one slow proxy does not starve others.
        """
        url = self.current()
        if url not in self._semaphores:
            self._semaphores[url] = asyncio.Semaphore(self._per_proxy_concurrency)
        return self._semaphores[url]

    async def get_session(self) -> aiohttp.ClientSession:
        """Return (or create) the session for the current active proxy."""
        url = self.current()
        if url not in self._sessions:
            self._sessions[url] = self._factory(url)
            LOG.debug("session created | url=%s", mask_proxy_url(url))
        return self._sessions[url]

    async def rotate(self, failed_url: str, reason: str) -> str | None:
        """Mark *failed_url* as failed in pool and switch to next proxy.

        Returns the new active proxy URL (or ``None`` if pool exhausted).
        """
        if self._pool is None:
            return None
        new_url: str | None = self._pool.mark_failed(failed_url, reason)
        if new_url is not None:
            await self._evict(failed_url)
            self._active_key = new_url
        return new_url

    async def set_active(self, url: str | None) -> None:
        """Explicitly set the active proxy URL (bypass pool)."""
        old = self._active_key
        self._active_key = url
        if old != url:
            await self._evict(old)

    async def refresh_pool(self, urls: list[str]) -> None:
        """Replace the pool with *urls*; evict sessions for removed URLs."""
        old_urls: set[str | None] = set()
        if self._pool is not None:
            old_urls = set(self._pool.urls)
            self._pool.reset(urls)
            self._active_key = self._pool.current()
        elif urls:
            self._active_key = urls[0]
        for url in old_urls - set(urls):
            await self._evict(url)

    async def replace_pool(self, pool: ProxyPool | None) -> None:
        """Replace the internal pool reference and evict orphaned sessions."""
        old_urls: set[str | None] = set()
        if self._pool is not None:
            old_urls = set(self._pool.urls)
        self._pool = pool
        new_urls = set(pool.urls) if pool is not None else set()
        for url in old_urls - new_urls:
            await self._evict(url)

    async def close_all(self) -> None:
        """Close all managed sessions and clear the cache."""
        for s in self._sessions.values():
            await close_aiohttp_session(s)
        self._sessions.clear()
        self._semaphores.clear()
        self._active_key = None

    def snapshot(self) -> dict[str, object]:
        return {
            "active": mask_proxy_url(self._active_key),
            "cached_sessions": list(self._sessions.keys()),
            "per_proxy_concurrency": self._per_proxy_concurrency,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _evict(self, url: str | None) -> None:
        s = self._sessions.pop(url, None)
        if s is not None:
            await close_aiohttp_session(s)
        self._semaphores.pop(url, None)
        if s is not None:
            LOG.debug("session evicted | url=%s", mask_proxy_url(url))

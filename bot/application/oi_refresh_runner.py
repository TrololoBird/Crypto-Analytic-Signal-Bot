from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..market_data import BinanceFuturesMarketData

LOG = logging.getLogger("bot.application.oi_refresh_runner")
_DEGRADATION_ERRORS = (RuntimeError, ValueError, TypeError, AttributeError, KeyError)


class OIRefreshRunner:
    """Periodic OI/L-S cache warmup loop for shortlist symbols."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._last_refresh_monotonic: float = 0.0

    async def run(self) -> None:
        await asyncio.sleep(30)  # stagger after shortlist populates
        while not self._bot._shutdown.is_set():
            async with self._bot._shortlist_lock:
                shortlist = list(self._bot._shortlist)

            await self.refresh_once(shortlist, max_age_seconds=0.0)

            # Increased interval from 15min to 30min to reduce API load
            # 45 symbols × 3 requests = 135 requests every 30min instead of every 15min
            try:
                await asyncio.wait_for(self._bot._shutdown.wait(), timeout=1800)
            except asyncio.TimeoutError:
                continue

    async def refresh_once(
        self,
        shortlist: list[Any],
        *,
        max_age_seconds: float = 300.0,
    ) -> int:
        if not shortlist or not isinstance(self._bot.client, BinanceFuturesMarketData):
            return 0
        now = time.monotonic()
        if max_age_seconds > 0 and now - self._last_refresh_monotonic < max_age_seconds:
            return 0

        batch_size = self._bot.settings.runtime.startup_batch_size
        batch_delay = self._bot.settings.runtime.startup_batch_delay_seconds
        rest_concurrency = max(
            1, int(self._bot.settings.runtime.max_concurrent_rest_requests)
        )
        sem = asyncio.Semaphore(rest_concurrency)

        async def _fetch_one(symbol: str, limiter: asyncio.Semaphore) -> None:
            async with limiter:
                await self._safe_fetch(symbol)

        processed = 0
        for i in range(0, len(shortlist), batch_size):
            batch = shortlist[i : i + batch_size]
            await asyncio.gather(
                *[_fetch_one(item.symbol, sem) for item in batch],
                return_exceptions=True,
            )
            processed += len(batch)
            if i + batch_size < len(shortlist):
                await asyncio.sleep(batch_delay)

        self._last_refresh_monotonic = time.monotonic()
        LOG.info(
            "oi/ls cache refreshed | symbols=%d batches=%d rest_concurrency=%d",
            processed,
            (len(shortlist) + batch_size - 1) // batch_size,
            rest_concurrency,
        )
        await self._bot._update_memory_market_context(shortlist)
        return processed

    async def _safe_fetch(self, symbol: str) -> None:
        client = self._bot.client

        # Skip if circuit breaker is open for critical operations
        if hasattr(client, "_is_circuit_open") and client._is_circuit_open(
            "open_interest_statistics"
        ):
            LOG.debug("skipping OI fetch for %s: circuit breaker open", symbol)
            return

        # Public-only derivatives context warmup. Keep it bounded, but include the
        # crowding ratios that the runtime can consume from cache.
        fetchers = (
            (
                "rest",
                "oi_change_1h",
                lambda: client.fetch_open_interest_change(symbol, period="1h"),
            ),
            (
                "rest",
                "top_account_ls_ratio_1h",
                lambda: client.fetch_long_short_ratio(symbol, period="1h"),
            ),
            (
                "rest",
                "top_position_ls_ratio_1h",
                lambda: client.fetch_top_position_ls_ratio(symbol, period="1h"),
            ),
            (
                "rest",
                "global_ls_ratio_1h",
                lambda: client.fetch_global_ls_ratio(symbol, period="1h"),
            ),
            (
                "rest",
                "funding_rate_history",
                lambda: client.fetch_funding_rate_history(symbol),
            ),
        )
        for source, stage, fetch in fetchers:
            degraded = False
            degrade_reason: str | None = None
            fallback_used = "none"
            try:
                await fetch()
            except _DEGRADATION_ERRORS as exc:
                degraded = True
                degrade_reason = str(exc)
                fallback_used = "skip_stage"
                LOG.warning(
                    "oi refresh degraded | symbol=%s stage=%s source=%s degraded=%s degrade_reason=%s fallback_used=%s exception_type=%s",
                    symbol,
                    stage,
                    source,
                    degraded,
                    degrade_reason,
                    fallback_used,
                    type(exc).__name__,
                )

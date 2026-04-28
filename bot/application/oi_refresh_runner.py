from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..market_data import BinanceFuturesMarketData

LOG = logging.getLogger("bot.application.oi_refresh_runner")


class OIRefreshRunner:
    """Periodic OI/L-S cache warmup loop for shortlist symbols."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def run(self) -> None:
        await asyncio.sleep(30)  # stagger after shortlist populates
        while not self._bot._shutdown.is_set():
            async with self._bot._shortlist_lock:
                shortlist = list(self._bot._shortlist)

            if shortlist and isinstance(self._bot.client, BinanceFuturesMarketData):
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

                LOG.info(
                    "oi/ls cache refreshed | symbols=%d batches=%d rest_concurrency=%d",
                    processed,
                    (len(shortlist) + batch_size - 1) // batch_size,
                    rest_concurrency,
                )
                await self._bot._update_memory_market_context(shortlist)

            # Increased interval from 15min to 30min to reduce API load
            # 45 symbols × 3 requests = 135 requests every 30min instead of every 15min
            try:
                await asyncio.wait_for(self._bot._shutdown.wait(), timeout=1800)
            except asyncio.TimeoutError:
                continue

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
            lambda: client.fetch_open_interest_change(symbol, period="1h"),
            lambda: client.fetch_long_short_ratio(symbol, period="1h"),
            lambda: client.fetch_top_position_ls_ratio(symbol, period="1h"),
            lambda: client.fetch_global_ls_ratio(symbol, period="1h"),
            lambda: client.fetch_funding_rate_history(symbol),
        )
        for fetch in fetchers:
            try:
                await fetch()
            except Exception:
                pass

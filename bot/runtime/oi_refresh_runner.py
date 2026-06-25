from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from engine.market.data import BinanceFuturesMarketData

LOG = logging.getLogger("bot.runtime.oi_refresh_runner")
_DEGRADATION_ERRORS = (
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    TimeoutError,
    asyncio.TimeoutError,
)
_DEFAULT_PRIORITY_CONTEXT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XAUUSDT",
    "XAGUSDT",
    "PAXGUSDT",
)
_PERSISTENT_SCALAR_STAGES: dict[str, int] = {
    "oi_current": 1800,
    "oi_change_1h": 3600,
    "top_account_ls_ratio_1h": 3600,
    "top_position_ls_ratio_1h": 3600,
    "global_ls_ratio_1h": 3600,
    "funding_rate": 3600,
    "basis_1h": 3600,
    "basis_5m": 1800,
}


class OIRefreshRunner:
    """Periodic OI/L-S cache warmup loop for shortlist symbols."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._last_refresh_monotonic: float = 0.0
        self._single_symbol_limiter: asyncio.Semaphore | None = None

    def _priority_symbol_set(self) -> set[str]:
        settings = getattr(self._bot, "settings", None)
        universe = getattr(settings, "universe", None)
        assets = getattr(settings, "assets", {}) or {}
        symbols = {symbol.upper() for symbol in _DEFAULT_PRIORITY_CONTEXT_SYMBOLS}
        if universe is not None:
            symbols.update(
                str(symbol).strip().upper()
                for symbol in getattr(universe, "pinned_symbols", ()) or ()
                if str(symbol).strip()
            )
        if isinstance(assets, dict):
            for symbol, config in assets.items():
                if bool(getattr(config, "deep_analysis", False)):
                    symbols.add(str(symbol).strip().upper())
        return symbols

    def _prioritized_shortlist(
        self,
        shortlist: list[Any],
        *,
        symbol_limit: int | None,
    ) -> list[Any]:
        priority = self._priority_symbol_set()
        seen: set[str] = set()
        priority_items: list[Any] = []
        normal_items: list[Any] = []
        for item in shortlist:
            symbol = str(getattr(item, "symbol", "") or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            if symbol in priority:
                priority_items.append(item)
            else:
                normal_items.append(item)
        ordered = [*priority_items, *normal_items]
        if symbol_limit is None or symbol_limit <= 0:
            return ordered
        limit = int(symbol_limit)
        protected = ordered[: len(priority_items)]
        room = max(limit - len(protected), 0)
        return [*protected, *normal_items[:room]]

    async def run(self) -> None:
        await asyncio.sleep(30)  # stagger after shortlist populates
        while not self._bot._shutdown.is_set():
            async with self._bot._shortlist_lock:
                shortlist = list(self._bot._shortlist)

            await self.refresh_once(shortlist, max_age_seconds=0.0)

            runtime = getattr(getattr(self._bot, "settings", None), "runtime", None)
            interval_minutes = int(getattr(runtime, "oi_refresh_interval_minutes", 30) or 30)
            sleep_seconds = max(300.0, float(interval_minutes) * 60.0)
            try:
                await asyncio.wait_for(self._bot._shutdown.wait(), timeout=sleep_seconds)
            except TimeoutError:
                continue

    async def refresh_once(
        self,
        shortlist: list[Any],
        *,
        max_age_seconds: float = 300.0,
        time_budget_seconds: float | None = None,
        symbol_limit: int | None = None,
        include_funding_history: bool = True,
        per_symbol_timeout_seconds: float | None = None,
    ) -> int:
        if not shortlist or not isinstance(self._bot.client, BinanceFuturesMarketData):
            return 0
        now = time.monotonic()
        if max_age_seconds > 0 and now - self._last_refresh_monotonic < max_age_seconds:
            return 0

        shortlist = self._prioritized_shortlist(shortlist, symbol_limit=symbol_limit)
        if not shortlist:
            return 0
        try:
            await self._bot.client.fetch_funding_info_all()
        except _DEGRADATION_ERRORS as exc:
            LOG.debug("funding info refresh skipped | error=%s", exc)

        deadline = (
            time.monotonic() + max(0.0, float(time_budget_seconds))
            if time_budget_seconds is not None and time_budget_seconds > 0
            else None
        )

        batch_size = self._bot.settings.runtime.startup_batch_size
        batch_delay = self._bot.settings.runtime.startup_batch_delay_seconds
        rest_concurrency = max(1, int(self._bot.settings.runtime.max_concurrent_rest_requests))
        sem = asyncio.Semaphore(rest_concurrency)

        async def _fetch_one(symbol: str, limiter: asyncio.Semaphore) -> None:
            async with limiter:
                timeout = per_symbol_timeout_seconds
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    timeout = remaining if timeout is None else min(timeout, remaining)
                try:
                    if timeout is not None and timeout > 0:
                        await asyncio.wait_for(
                            self._safe_fetch(
                                symbol,
                                include_funding_history=include_funding_history,
                                priority_symbol=symbol in self._priority_symbol_set(),
                            ),
                            timeout=timeout,
                        )
                    else:
                        await self._safe_fetch(
                            symbol,
                            include_funding_history=include_funding_history,
                            priority_symbol=symbol in self._priority_symbol_set(),
                        )
                except TimeoutError as exc:
                    LOG.info(
                        (
                            "oi refresh symbol skipped | symbol=%s "
                            "reason=context_fetch_timeout detail=%s exception_type=%s"
                        ),
                        symbol,
                        str(exc),
                        type(exc).__name__,
                    )
                except _DEGRADATION_ERRORS as exc:
                    LOG.info(
                        (
                            "oi refresh symbol skipped | symbol=%s "
                            "reason=context_fetch_error detail=%s exception_type=%s"
                        ),
                        symbol,
                        str(exc),
                        type(exc).__name__,
                    )

        processed = 0
        budget_reached = False
        for i in range(0, len(shortlist), batch_size):
            if deadline is not None and time.monotonic() >= deadline:
                budget_reached = True
                LOG.info(
                    (
                        "oi/ls cache refresh partial | attempted=%d total=%d "
                        "budget_s=%.1f reason=time_budget"
                    ),
                    processed,
                    len(shortlist),
                    float(time_budget_seconds or 0.0),
                )
                break
            batch = shortlist[i : i + batch_size]
            await asyncio.gather(
                *[_fetch_one(item.symbol, sem) for item in batch],
                return_exceptions=True,
            )
            processed += len(batch)
            if i + batch_size < len(shortlist):
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        continue
                    await asyncio.sleep(min(batch_delay, remaining))
                else:
                    await asyncio.sleep(batch_delay)

        self._last_refresh_monotonic = time.monotonic()
        LOG.info(
            (
                "oi/ls cache refreshed | symbols=%d total=%d batches=%d "
                "rest_concurrency=%d funding_history=%s partial=%s"
            ),
            processed,
            len(shortlist),
            (len(shortlist) + batch_size - 1) // batch_size,
            rest_concurrency,
            include_funding_history,
            budget_reached,
        )
        await self._bot._update_memory_market_context(shortlist)
        return processed

    async def refresh_symbol_if_missing(
        self,
        symbol: str,
        *,
        max_age_seconds: float = 900.0,
        include_funding_history: bool = False,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Warm derivatives context for one symbol when the cache is incomplete."""
        if not symbol or not isinstance(self._bot.client, BinanceFuturesMarketData):
            return False
        client = self._bot.client
        has_oi_change = client.get_cached_oi_change(symbol, max_age_s=max_age_seconds) is not None
        has_ls = client.get_cached_ls_ratio(symbol, max_age_s=max_age_seconds) is not None
        has_top_position_ls = (
            client.get_cached_top_position_ls_ratio(symbol, max_age_s=max_age_seconds) is not None
        )
        has_global_ls = (
            client.get_cached_global_ls_ratio(symbol, max_age_s=max_age_seconds) is not None
        )
        has_taker = client.get_cached_taker_ratio(symbol, max_age_s=max_age_seconds) is not None
        has_funding = client.get_cached_funding_rate(symbol, max_age_s=max_age_seconds) is not None
        has_funding_history = (
            client.get_cached_funding_trend(symbol, max_age_s=max_age_seconds) is not None
        )
        if (
            has_oi_change
            and has_ls
            and has_top_position_ls
            and has_global_ls
            and has_taker
            and has_funding
            and (has_funding_history or not include_funding_history)
        ):
            return False
        limiter = self._get_single_symbol_limiter()
        if limiter.locked():
            LOG.debug(
                (
                    "symbol derivatives context warmup skipped | symbol=%s "
                    "reason=single_symbol_limiter_saturated"
                ),
                symbol,
            )
            return False
        acquired_limiter = False
        try:
            try:
                await asyncio.wait_for(limiter.acquire(), timeout=0.05)
                acquired_limiter = True
            except TimeoutError:
                LOG.debug(
                    (
                        "symbol derivatives context warmup skipped | symbol=%s "
                        "reason=single_symbol_limiter_busy"
                    ),
                    symbol,
                )
                return False
            if timeout_seconds is not None and timeout_seconds > 0:
                await asyncio.wait_for(
                    self._safe_fetch(
                        symbol,
                        include_funding_history=include_funding_history,
                        priority_symbol=symbol in self._priority_symbol_set(),
                    ),
                    timeout=timeout_seconds,
                )
            else:
                await self._safe_fetch(
                    symbol,
                    include_funding_history=include_funding_history,
                    priority_symbol=symbol in self._priority_symbol_set(),
                )
        except TimeoutError as exc:
            LOG.debug(
                (
                    "symbol derivatives context warmup skipped | symbol=%s "
                    "reason=context_fetch_timeout exception_type=%s"
                ),
                symbol,
                type(exc).__name__,
            )
            return False
        except _DEGRADATION_ERRORS as exc:
            LOG.info(
                (
                    "symbol derivatives context warmup skipped | symbol=%s "
                    "reason=context_fetch_error detail=%s exception_type=%s"
                ),
                symbol,
                str(exc),
                type(exc).__name__,
            )
            return False
        else:
            return True
        finally:
            if acquired_limiter:
                limiter.release()

    def _get_single_symbol_limiter(self) -> asyncio.Semaphore:
        limiter = self._single_symbol_limiter
        if limiter is not None:
            return limiter
        runtime = getattr(getattr(self._bot, "settings", None), "runtime", None)
        configured = int(getattr(runtime, "max_concurrent_rest_requests", 1) or 1)
        capacity = max(1, min(2, configured))
        self._single_symbol_limiter = asyncio.Semaphore(capacity)
        return self._single_symbol_limiter

    async def _hydrate_market_cache(self, client: Any, repo: Any, symbol: str) -> None:
        if hasattr(client, "seed_funding_history_cache"):
            cache_key = f"funding_rate_history:{symbol}"
            try:
                cached_payload = await repo.read_market_cache(cache_key, max_age_s=7200.0)
                if cached_payload:
                    rows = json.loads(cached_payload)
                    if isinstance(rows, list):
                        client.seed_funding_history_cache(symbol, rows)
            except (TypeError, ValueError, json.JSONDecodeError):
                LOG.debug("market cache hydrate skipped | symbol=%s stage=funding_history", symbol)
        if not hasattr(client, "seed_market_scalar_cache"):
            return
        for stage, ttl_seconds in _PERSISTENT_SCALAR_STAGES.items():
            cache_key = f"{stage}:{symbol}"
            try:
                cached_payload = await repo.read_market_cache(
                    cache_key,
                    max_age_s=float(ttl_seconds),
                )
                if not cached_payload:
                    continue
                payload = json.loads(cached_payload)
                value = payload.get("value") if isinstance(payload, dict) else payload
                if isinstance(value, (int, float)):
                    client.seed_market_scalar_cache(stage, symbol, float(value))
            except (TypeError, ValueError, json.JSONDecodeError):
                LOG.debug("market cache hydrate skipped | symbol=%s stage=%s", symbol, stage)

    async def _persist_market_cache(
        self,
        repo: Any,
        symbol: str,
        stage: str,
        result: Any,
    ) -> None:
        if stage == "funding_rate_history" and isinstance(result, list) and result:
            await repo.write_market_cache(
                f"funding_rate_history:{symbol}",
                json.dumps(result, separators=(",", ":")),
                ttl_seconds=7200,
            )
            return
        ttl_seconds = _PERSISTENT_SCALAR_STAGES.get(stage)
        if ttl_seconds is None or not isinstance(result, (int, float)):
            return
        await repo.write_market_cache(
            f"{stage}:{symbol}",
            json.dumps({"value": float(result)}, separators=(",", ":")),
            ttl_seconds=int(ttl_seconds),
        )

    async def _safe_fetch(
        self,
        symbol: str,
        *,
        include_funding_history: bool = True,
        priority_symbol: bool = False,
    ) -> None:
        client = self._bot.client

        # Skip if circuit breaker is open for critical operations
        if hasattr(client, "_is_circuit_open") and client._is_circuit_open(
            "open_interest_statistics"
        ):
            LOG.debug("skipping OI fetch for %s: circuit breaker open", symbol)
            return

        # Public-only derivatives context warmup. Keep it bounded, but include the
        # crowding ratios that the runtime can consume from cache.
        fetchers: list[tuple[str, str, Any]] = [
            (
                "rest",
                "oi_current",
                lambda: client.fetch_open_interest(symbol),
            ),
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
                "taker_ratio_1h",
                lambda: client.fetch_taker_ratio(symbol, period="1h"),
            ),
            (
                "rest",
                "global_ls_ratio_1h",
                lambda: client.fetch_global_ls_ratio(symbol, period="1h"),
            ),
            (
                "rest",
                "funding_rate",
                lambda: client.fetch_funding_rate(symbol),
            ),
            (
                "rest",
                "basis_1h",
                lambda: client.fetch_basis(symbol, period="1h", limit=6),
            ),
            (
                "rest",
                "basis_5m",
                lambda: client.fetch_basis(symbol, period="5m", limit=12),
            ),
        ]
        if include_funding_history:
            fetchers.append(
                (
                    "rest",
                    "funding_rate_history",
                    lambda: client.fetch_funding_rate_history(symbol),
                )
            )
        if priority_symbol:
            fetchers.extend(
                (
                    (
                        "rest",
                        "priority_history_15m",
                        lambda: client.fetch_priority_history_bundle(
                            symbol,
                            intervals=("15m",),
                            limit=300,
                        ),
                    ),
                    (
                        "rest",
                        "priority_history_1h_4h",
                        lambda: client.fetch_priority_history_bundle(
                            symbol,
                            intervals=("1h", "4h"),
                            limit=240,
                        ),
                    ),
                )
            )
        repo = getattr(self._bot, "_modern_repo", None)
        if repo is not None:
            await self._hydrate_market_cache(client, repo, symbol)

        for source, stage, fetch in fetchers:
            try:
                result = await fetch()
                if repo is not None:
                    await self._persist_market_cache(repo, symbol, stage, result)
            except _DEGRADATION_ERRORS as exc:
                LOG.info(
                    (
                        "oi refresh optional stage skipped | symbol=%s stage=%s source=%s "
                        "reason=context_fetch_error detail=%s exception_type=%s"
                    ),
                    symbol,
                    stage,
                    source,
                    str(exc),
                    type(exc).__name__,
                )


async def run_oi_refresh_loop(runner: OIRefreshRunner) -> None:
    """Background OI/L-S refresh loop (started from SignalBot.run_forever)."""
    await runner.run()

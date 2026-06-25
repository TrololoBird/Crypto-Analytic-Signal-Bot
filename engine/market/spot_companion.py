"""Public Binance spot quotes for futures lead-lag and spot/futures spread.

Spot REST uses a dedicated aiohttp session and does not share the futures
``_WeightBudgetManager`` in ``bot.market.rate_limit`` (``REST_WEIGHT_PACE_LIMIT``).
Keep ``[bot.spot_companion]`` refresh intervals conservative to avoid spot IP bans.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from engine.errors import DEFENSIVE_EXC
from engine.market.data import _HTTP_CONNECTOR_LIMIT
from engine.market.network_proxy import aiohttp_request_proxy, create_aiohttp_session

LOG = logging.getLogger("bot.market.spot_companion")

_SPOT_DEFAULT_BASE_URL = "https://data-api.binance.vision"
_SPOT_KLINE_PATH = "/api/v3/klines"
_SPOT_PRICE_PATH = "/api/v3/ticker/price"
_DEFAULT_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True, slots=True)
class SpotMetrics:
    symbol: str
    spot_price: float
    spot_lead_return_1m: float | None
    spot_futures_spread_bps: float | None
    fetched_at: float


class SpotCompanionService:
    """Caches spot metrics for USD-M symbols (same symbol on spot market)."""

    def __init__(
        self,
        *,
        base_url: str = _SPOT_DEFAULT_BASE_URL,
        proxy_url: str | None = None,
        trust_env: bool = True,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = str(base_url or _SPOT_DEFAULT_BASE_URL).rstrip("/")
        self._proxy_url = proxy_url
        self._trust_env = trust_env
        self._timeout = float(timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, SpotMetrics] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            self._session = create_aiohttp_session(
                proxy_url=self._proxy_url,
                trust_env=self._trust_env,
                timeout=timeout,
                connector_limit=_HTTP_CONNECTOR_LIMIT,
            )
        return self._session

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        session = await self._ensure_session()
        url = f"{self._base_url}{path}"
        request_proxy = aiohttp_request_proxy(session, self._proxy_url)
        async with session.get(url, params=params, proxy=request_proxy) as response:
            response.raise_for_status()
            return await response.json()

    @staticmethod
    def _lead_return_1m(klines: list[Any]) -> float | None:
        if len(klines) < 2:
            return None
        try:
            prev_close = float(klines[-2][4])
            last_close = float(klines[-1][4])
        except (IndexError, TypeError, ValueError):
            return None
        if prev_close <= 0.0:
            return None
        return (last_close - prev_close) / prev_close * 100.0

    @staticmethod
    def _spread_bps(spot_price: float, futures_mid: float | None) -> float | None:
        if futures_mid is None or spot_price <= 0.0 or futures_mid <= 0.0:
            return None
        return (futures_mid - spot_price) / spot_price * 10_000.0

    async def fetch_symbol_metrics(
        self,
        symbol: str,
        *,
        futures_mid: float | None = None,
    ) -> SpotMetrics | None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        try:
            price_payload = await self._get_json(_SPOT_PRICE_PATH, {"symbol": sym})
            spot_price = float(price_payload.get("price") or 0.0)
            if spot_price <= 0.0:
                return None
            klines = await self._get_json(
                _SPOT_KLINE_PATH,
                {"symbol": sym, "interval": "1m", "limit": 2},
            )
            if not isinstance(klines, list):
                klines = []
            lead = self._lead_return_1m(klines)
            spread = self._spread_bps(spot_price, futures_mid)
            return SpotMetrics(
                symbol=sym,
                spot_price=spot_price,
                spot_lead_return_1m=lead,
                spot_futures_spread_bps=spread,
                fetched_at=time.monotonic(),
            )
        except DEFENSIVE_EXC as exc:
            LOG.debug("spot companion fetch failed | symbol=%s error=%s", sym, exc)
            return None

    async def refresh_symbols(
        self,
        symbols: list[str],
        *,
        futures_mid_by_symbol: dict[str, float | None] | None = None,
        concurrency: int = 6,
    ) -> int:
        mids = futures_mid_by_symbol or {}
        sem = asyncio.Semaphore(max(1, int(concurrency)))
        updated = 0

        async def _one(symbol: str) -> None:
            nonlocal updated
            async with sem:
                metrics = await self.fetch_symbol_metrics(
                    symbol,
                    futures_mid=mids.get(symbol.upper()),
                )
                if metrics is None:
                    return
                async with self._lock:
                    self._cache[metrics.symbol] = metrics
                updated += 1

        await asyncio.gather(
            *[_one(sym) for sym in symbols if str(sym).strip()], return_exceptions=True
        )
        return updated

    def enrichments_for(self, symbol: str, *, max_age_seconds: float = 120.0) -> dict[str, float]:
        sym = str(symbol or "").strip().upper()
        metrics = self._cache.get(sym)
        if metrics is None:
            return {}
        if time.monotonic() - metrics.fetched_at > max_age_seconds:
            return {}
        payload: dict[str, float] = {}
        if metrics.spot_lead_return_1m is not None:
            payload["spot_lead_return_1m"] = metrics.spot_lead_return_1m
        if metrics.spot_futures_spread_bps is not None:
            payload["spot_futures_spread_bps"] = metrics.spot_futures_spread_bps
        return payload

    def cache_size(self) -> int:
        return len(self._cache)

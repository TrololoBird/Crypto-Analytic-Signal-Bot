"""Binance public kline fetcher for SL forensic analysis."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Self

import aiohttp

LOG = logging.getLogger("sl_forensic.fetcher")

# Interval duration in milliseconds for window sizing.
_INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
_TF_MAP: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "1d": "1d",
}


def _resolve_proxy(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        from bot.domain.config import load_settings

        settings = load_settings()
        urls = settings.network.effective_proxy_urls()
        return urls[0] if urls else None
    except Exception:
        return None


class CandleFetcher:
    """Fetches historical klines from Binance USDⓈ-M futures public API."""

    ENDPOINT = "https://fapi.binance.com/fapi/v1/klines"

    def __init__(self, proxy: str | None = None) -> None:
        self._proxy = _resolve_proxy(proxy)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    def _interval_ms(self, interval: str) -> int:
        mapped = _TF_MAP.get(interval, interval)
        return _INTERVAL_MS.get(mapped, 900_000)

    async def _request_klines(
        self,
        *,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        limit: int,
    ) -> list[list[Any]]:
        session = await self._get_session()
        params = {
            "symbol": symbol.upper(),
            "interval": _TF_MAP.get(interval, interval),
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": min(max(1, limit), 1500),
        }
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with session.get(
                    self.ENDPOINT,
                    params=params,
                    proxy=self._proxy,
                ) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        wait = 2**attempt
                        if resp.status == 429:
                            wait = max(wait, 60)
                        LOG.warning(
                            "klines retry | symbol=%s status=%d attempt=%d wait=%ds",
                            symbol,
                            resp.status,
                            attempt + 1,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    payload = await resp.json()
                    if isinstance(payload, list):
                        return payload
                    last_exc = TypeError(f"unexpected klines payload type: {type(payload)}")
                    await asyncio.sleep(2**attempt)
            except (TimeoutError, aiohttp.ClientError, TypeError) as exc:
                last_exc = exc
                await asyncio.sleep(2**attempt)
        if last_exc is not None:
            raise last_exc
        return []

    @staticmethod
    def _parse_row(row: list[Any], *, closed: bool) -> dict[str, Any]:
        return {
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "closed": closed,
        }

    async def fetch(
        self,
        symbol: str,
        interval: str,
        anchor_ts_ms: int,
        before_count: int = 60,
        after_count: int = 60,
    ) -> list[dict[str, Any]]:
        """Fetch candles centered on anchor timestamp."""
        step = self._interval_ms(interval)
        start_ms = anchor_ts_ms - before_count * step
        end_ms = anchor_ts_ms + after_count * step
        limit = before_count + after_count + 5
        rows = await self._request_klines(
            symbol=symbol,
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=limit,
        )
        await asyncio.sleep(0.15)
        candles: list[dict[str, Any]] = []
        for idx, row in enumerate(rows):
            is_last = idx == len(rows) - 1
            candles.append(self._parse_row(row, closed=not is_last))
        return candles

    async def fetch_window(
        self,
        symbol: str,
        anchor_ts_ms: int,
        sl_hit_ts_ms: int,
        signal_tf: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch all timeframes needed for one forensic case."""
        tf = signal_tf.split("+", maxsplit=1)[0].strip() or "15m"
        results: dict[str, list[dict[str, Any]]] = {}
        results[tf] = await self.fetch(symbol, tf, anchor_ts_ms, 60, 60)
        results["1h"] = await self.fetch(symbol, "1h", anchor_ts_ms, 24, 24)
        results["4h"] = await self.fetch(symbol, "4h", anchor_ts_ms, 12, 12)
        results["BTC_signal_tf"] = await self.fetch("BTCUSDT", tf, sl_hit_ts_ms, 5, 5)
        return results

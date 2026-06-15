"""Hunter REST market client — CCXT ``binance`` + defaultType future (public only)."""
from __future__ import annotations



import asyncio
import logging
import math
import time
from typing import Any

import ccxt.async_support as ccxt
import polars as pl

from hunt_core.errors import finite_float_or_none
from hunt_core.domain.schemas import AggTradeSnapshot, SymbolMeta
from hunt_core.market.factory import (
    close_exchange_async,
    create_async_binance_future,
    create_async_secondary_swap,
    create_pro_binance_future,
)
from hunt_core.market.factory import ccxt_ohlcv_to_frame, finalize_kline_frame
from hunt_core.market.symbols import (
    from_ccxt_symbol,
    is_linear_usdt_swap_market,
    resolve_linear_usdt_swap,
    to_binance_symbol,
    to_ccxt_symbol,
    try_binance_id_from_ccxt,
)

LOG = logging.getLogger("hunt_core.market.client")

_CACHE_TTL: dict[str, int] = {
    "klines_1m": 25,
    "klines_3m": 120,
    "klines_5m": 45,
    "klines_15m": 900,
    "klines_1h": 3900,
    "klines_4h": 14400,
    "klines_1d": 3600,
    "open_interest": 600,
    "open_interest_change": 600,
    "metric_series": 240,
    "long_short_ratio": 600,
    "funding_rate": 300,
    "funding_history": 1800,
    "funding_info": 3600,
    "basis": 1800,
    "book_ticker": 5,
    "order_book_depth": 5,
    "ticker_24h": 15,
    "exchange_info": 3600,
    "taker_ratio": 1200,
    "leverage_tiers": 3600,
    "secondary_funding": 600,
    "secondary_oi": 600,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


class HuntCcxtClient:
    """Drop-in for ``BinanceFuturesMarketData`` across hunt runtime/scripts."""

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        trust_env: bool = True,
        timeout_ms: int = 45_000,
    ) -> None:
        self._proxy_url = proxy_url
        self._trust_env = trust_env
        self._timeout_ms = timeout_ms
        self._ex: ccxt.binance = create_async_binance_future(
            proxy_url=proxy_url,
            trust_env=trust_env,
            timeout_ms=timeout_ms,
        )
        self._pro_ex: Any | None = None
        self._pro_lock = asyncio.Lock()
        self._markets_loaded = False
        self._klines_cache: dict[tuple[str, str, int], tuple[float, pl.DataFrame]] = {}
        self._klines_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        self._ticker_24h_cache: tuple[float, list[dict[str, float | str]]] | None = None
        self._exchange_info_cache: tuple[float, list[SymbolMeta]] | None = None
        self._open_interest_cache: dict[str, tuple[float, float]] = {}
        self._open_interest_change_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._long_short_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._top_position_ls_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._global_ls_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._taker_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._funding_rate_cache: dict[str, tuple[float, float]] = {}
        self._funding_history_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._funding_info_all_cache: tuple[float, dict[str, dict[str, float | int]]] | None = None
        self._premium_index_all_cache: tuple[float, dict[str, dict[str, float]]] | None = None
        self._basis_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._basis_stats_cache: dict[tuple[str, str], tuple[float, dict[str, float | None]]] = {}
        self._basis_api_unsupported: set[str] = set()
        self._oi_series_cache: dict[tuple[str, str, int], tuple[float, list[float]]] = {}
        self._gls_series_cache: dict[tuple[str, str, int], tuple[float, list[float]]] = {}
        self._order_book_depth_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
        self._leverage_tiers_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._leverage_tiers_skip_logged = False
        self._secondary_funding_cache: dict[tuple[str, str], tuple[float, dict[str, float | None]]] = {}
        self._secondary_oi_cache: dict[tuple[str, str], tuple[float, dict[str, float | None]]] = {}
        self._secondary_clients: dict[str, ccxt.Exchange] = {}
        self._secondary_failed: set[str] = set()
        self._secondary_lock = asyncio.Lock()
        from hunt_core.market.cross import configured_secondary_exchanges
        # HUNT_CROSS_EXCHANGES override; ids reused verbatim as ccxt exchange ids.
        self._secondary_exchange_ids: dict[str, str] = {
            name: name for name in configured_secondary_exchanges()
        }

    @classmethod
    def from_settings(cls, settings: Any) -> HuntCcxtClient:
        net = getattr(settings, "network", settings)
        return cls(
            proxy_url=getattr(net, "proxy_url", None),
            trust_env=getattr(net, "trust_env", True),
        )

    @property
    def exchange(self) -> ccxt.binance:
        return self._ex

    def _share_markets_to(self, target: ccxt.binance) -> None:
        """Reuse bootstrapped REST markets on Pro/secondary CCXT instances."""
        if not self._markets_loaded or not self._ex.markets:
            return
        target.set_markets(list(self._ex.markets.values()))

    async def acquire_pro_exchange(self) -> Any:
        """Lazy CCXT Pro ``binance`` (future) — shared with ``HuntCcxtStreams``."""
        async with self._pro_lock:
            if self._pro_ex is None:
                self._pro_ex = create_pro_binance_future(
                    proxy_url=self._proxy_url,
                    trust_env=self._trust_env,
                    timeout_ms=self._timeout_ms,
                )
                if self._markets_loaded:
                    self._share_markets_to(self._pro_ex)
                else:
                    await self._pro_ex.load_markets()
            return self._pro_ex

    async def reset_pro_exchange(self) -> Any:
        """Close and recreate Pro client after WS transport failure (CCXT wiki pattern)."""
        async with self._pro_lock:
            if self._pro_ex is not None:
                await close_exchange_async(self._pro_ex, label="binance_pro_reset")
                self._pro_ex = None
            self._pro_ex = create_pro_binance_future(
                proxy_url=self._proxy_url,
                trust_env=self._trust_env,
                timeout_ms=self._timeout_ms,
            )
            if self._markets_loaded:
                self._share_markets_to(self._pro_ex)
            else:
                await self._pro_ex.load_markets()
            return self._pro_ex

    async def load_markets(self) -> None:
        if self._markets_loaded:
            return
        try:
            await self._ex.load_markets()
            self._markets_loaded = True
            return
        except Exception as exc:
            LOG.warning(
                "ccxt_load_markets_failed | proxy=%s err=%s — fapi bootstrap",
                self._proxy_url or "direct",
                type(exc).__name__,
            )
        await self._bootstrap_markets_via_fapi_http()
        self._markets_loaded = True

    async def _bootstrap_markets_via_fapi_http(self) -> None:
        """SOCKS-aware fapi exchangeInfo when CCXT load_markets transport fails."""
        import aiohttp

        from hunt_core.market.network import (
            aiohttp_request_proxy,
            close_aiohttp_session,
            create_aiohttp_session,
            mask_proxy_url,
        )

        timeout = aiohttp.ClientTimeout(total=25)
        session = create_aiohttp_session(
            proxy_url=self._proxy_url,
            trust_env=self._trust_env,
            timeout=timeout,
            connector_limit=8,
        )
        req_proxy = aiohttp_request_proxy(session, self._proxy_url)
        try:
            url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
            async with session.get(url, proxy=req_proxy) as resp:
                if resp.status == 418:
                    body = await resp.text()
                    raise ccxt.DDoSProtection(f"binance 418 {body[:200]}")
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
            symbols = payload.get("symbols") if isinstance(payload, dict) else None
            if not isinstance(symbols, list) or not symbols:
                raise RuntimeError("fapi_exchange_info_empty")
            markets = self._ex.parse_markets(symbols)
            self._ex.set_markets(markets)
            LOG.info(
                "hunt_markets_bootstrapped | via=fapi_http proxy=%s n=%d",
                mask_proxy_url(self._proxy_url) if self._proxy_url else "direct",
                len(markets),
            )
        finally:
            await close_aiohttp_session(session)

    async def close(self) -> None:
        for name, ex in list(self._secondary_clients.items()):
            await close_exchange_async(ex, label=f"secondary_rest:{name}")
        self._secondary_clients.clear()
        self._secondary_failed.clear()
        if self._pro_ex is not None:
            await close_exchange_async(self._pro_ex, label="binance_pro")
            self._pro_ex = None
        await close_exchange_async(self._ex, label="binance_rest")

    def _ccxt_sym(self, symbol: str) -> str:
        return to_ccxt_symbol(symbol, exchange=self._ex)

    def _bin_sym(self, symbol: str) -> str:
        return to_binance_symbol(symbol)

    @staticmethod
    def _cache_fresh(entry: tuple[float, Any] | None, ttl: float) -> bool:
        return entry is not None and (time.monotonic() - entry[0]) < ttl

    @staticmethod
    def _ccxt_has(exchange: ccxt.Exchange, method: str) -> bool:
        flag = getattr(exchange, "has", {}).get(method)
        return flag is True

    def _fapi_market_id(self, symbol: str) -> str:
        market = self._ex.market(self._ccxt_sym(symbol))
        return str(market.get("id") or self._bin_sym(symbol))

    @staticmethod
    def _fapi_latest_ratio(payload: Any, *keys: str) -> float | None:
        if not payload:
            return None
        rows = payload if isinstance(payload, list) else [payload]
        if not rows:
            return None
        item = rows[-1] if isinstance(rows[-1], dict) else {}
        for key in keys:
            value = finite_float_or_none(item.get(key))
            if value is not None and value > 0:
                return value
        return None

    async def _fetch_fapi_metric(
        self,
        symbol: str,
        *,
        period: str,
        fetcher: Any,
        ratio_keys: tuple[str, ...],
        cache: dict[tuple[str, str], tuple[float, float]],
        ttl_key: str,
    ) -> float | None:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period)
        now = time.monotonic()
        cached = cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL[ttl_key]):
            return cached[1]  # type: ignore[index]
        try:
            await self.load_markets()
            payload = await fetcher(
                {"symbol": self._fapi_market_id(sym), "period": period, "limit": 1}
            )
            value = self._fapi_latest_ratio(payload, *ratio_keys)
            if value is not None and value > 0:
                cache[cache_key] = (now, value)
                return value
        except Exception as exc:
            LOG.warning(
                "fapi_metric_failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
        return None

    async def fetch_exchange_symbols(self) -> list[SymbolMeta]:
        now = time.monotonic()
        if self._cache_fresh(self._exchange_info_cache, _CACHE_TTL["exchange_info"]):
            assert self._exchange_info_cache is not None
            return self._exchange_info_cache[1]
        await self.load_markets()
        rows: list[SymbolMeta] = []
        for market in self._ex.markets.values():
            info = market.get("info") if isinstance(market, dict) else None
            info = info if isinstance(info, dict) else {}
            rows.append(
                SymbolMeta(
                    symbol=str(market.get("id") or info.get("symbol") or ""),
                    base_asset=str(market.get("base") or info.get("baseAsset") or ""),
                    quote_asset=str(market.get("quote") or info.get("quoteAsset") or ""),
                    contract_type=str(info.get("contractType") or ""),
                    status=str(info.get("status") or ""),
                    onboard_date_ms=int(info.get("onboardDate") or 0),
                )
            )
        self._exchange_info_cache = (now, rows)
        return rows

    async def fetch_ticker_24h(self) -> list[dict[str, float | str]]:
        now = time.monotonic()
        if self._cache_fresh(self._ticker_24h_cache, _CACHE_TTL["ticker_24h"]):
            assert self._ticker_24h_cache is not None
            return self._ticker_24h_cache[1]
        await self.load_markets()
        tickers = await self._ex.fetch_tickers()
        rows: list[dict[str, float | str]] = []
        for ccxt_sym, item in tickers.items():
            if not is_linear_usdt_swap_market(self._ex.markets.get(ccxt_sym)):
                continue
            sym = try_binance_id_from_ccxt(ccxt_sym, exchange=self._ex)
            if not sym:
                continue
            last_price = _safe_float(item.get("last"))
            quote_volume = _safe_float(item.get("quoteVolume"))
            if not sym or last_price <= 0 or quote_volume <= 0:
                continue
            row: dict[str, float | str] = {
                "symbol": sym,
                "last_price": last_price,
                "price_change_percent": _safe_float(item.get("percentage")),
                "quote_volume": quote_volume,
                "trade_count": _safe_float(item.get("info", {}).get("count")),
            }
            high = _safe_float(item.get("high"))
            low = _safe_float(item.get("low"))
            if high > 0:
                row["high_price"] = high
            if low > 0:
                row["low_price"] = low
            rows.append(row)
        self._ticker_24h_cache = (now, rows)
        return rows

    async def fetch_ohlcv_list(
        self,
        symbol: str,
        interval: str,
        *,
        since: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        await self.load_markets()
        ccxt_sym = self._ccxt_sym(symbol)
        rows = await self._ex.fetch_ohlcv(
            ccxt_sym,
            interval,
            since=since,
            limit=min(1500, max(1, int(limit))),
        )
        return list(rows)

    async def fetch_klines(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        await self.load_markets()
        ccxt_sym = self._ccxt_sym(symbol)
        rows = await self._ex.fetch_ohlcv(ccxt_sym, interval, limit=max(1, int(limit)))
        frame = finalize_kline_frame(
            ccxt_ohlcv_to_frame(rows, interval, exchange=self._ex),
            interval,
            exchange=self._ex,
        )
        return frame

    async def fetch_klines_between(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1500,
    ) -> pl.DataFrame:
        await self.load_markets()
        ccxt_sym = self._ccxt_sym(symbol)
        rows = await self._ex.fetch_ohlcv(
            ccxt_sym,
            interval,
            since=max(0, int(start_time_ms)),
            limit=min(1500, max(1, int(limit))),
        )
        end_ms = max(0, int(end_time_ms))
        trimmed = [r for r in rows if r and int(r[0]) <= end_ms]
        return ccxt_ohlcv_to_frame(trimmed, interval, exchange=self._ex)

    async def fetch_klines_cached(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        key = (self._bin_sym(symbol), interval, int(limit))
        ttl = float(_CACHE_TTL.get(f"klines_{interval}", 60))
        now = time.monotonic()
        cached = self._klines_cache.get(key)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
        lock = self._klines_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._klines_cache.get(key)
            if cached is not None and now - cached[0] < ttl:
                return cached[1]
            frame = await self.fetch_klines(symbol, interval, limit=limit)
            self._klines_cache[key] = (time.monotonic(), frame)
            return frame

    def get_cached_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int,
        max_age_s: float | None = None,
    ) -> pl.DataFrame | None:
        key = (self._bin_sym(symbol), interval, int(limit))
        cached = self._klines_cache.get(key)
        if cached is None:
            return None
        ttl = max_age_s if max_age_s is not None else float(_CACHE_TTL.get(f"klines_{interval}", 60))
        if time.monotonic() - cached[0] > ttl:
            return None
        return cached[1]

    async def fetch_order_book_depth_snapshot(
        self, symbol: str, *, limit: int = 20
    ) -> dict[str, float | None]:
        from hunt_core.market.client import depth_snapshot_from_book

        sym = self._bin_sym(symbol)
        depth_limit = min(100, max(5, int(limit)))
        key = (sym, depth_limit)
        now = time.monotonic()
        cached = self._order_book_depth_cache.get(key)
        if self._cache_fresh(cached, _CACHE_TTL["order_book_depth"]):
            return dict(cached[1])  # type: ignore[index]
        await self.load_markets()
        ob = await self._ex.fetch_order_book(self._ccxt_sym(sym), limit=depth_limit)
        bids = [(float(row[0]), float(row[1])) for row in (ob.get("bids") or []) if row]
        asks = [(float(row[0]), float(row[1])) for row in (ob.get("asks") or []) if row]
        if not bids or not asks:
            return {"bid_price": None, "ask_price": None, "bid_qty": None, "ask_qty": None}
        snapshot: dict[str, Any] = depth_snapshot_from_book(bids, asks)
        snapshot["bids"] = bids
        snapshot["asks"] = asks
        snapshot["exchange"] = "binance"
        self._order_book_depth_cache[key] = (now, snapshot)
        return snapshot

    async def _fetch_book_ticker_rest_detail(self, symbol: str) -> dict[str, float | None]:
        depth = await self.fetch_order_book_depth_snapshot(symbol, limit=5)
        if depth.get("bid_price"):
            return depth
        return {"bid_price": None, "ask_price": None, "bid_qty": None, "ask_qty": None}

    async def fetch_open_interest(self, symbol: str) -> float | None:
        sym = self._bin_sym(symbol)
        now = time.monotonic()
        cached = self._open_interest_cache.get(sym)
        if self._cache_fresh(cached, _CACHE_TTL["open_interest"]):
            return cached[1]  # type: ignore[index]
        await self.load_markets()
        try:
            payload = await self._ex.fetch_open_interest(self._ccxt_sym(sym))
            value = _safe_float(payload.get("openInterestAmount") or payload.get("info", {}).get("openInterest"))
            if value > 0:
                self._open_interest_cache[sym] = (now, value)
                return value
        except Exception as exc:
            LOG.warning("fetch_open_interest failed | symbol=%s error=%s", sym, exc)
        return None

    async def fetch_open_interest_change(self, symbol: str, *, period: str = "1h") -> float | None:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period)
        now = time.monotonic()
        cached = self._open_interest_change_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["open_interest_change"]):
            return cached[1]  # type: ignore[index]
        try:
            await self.load_markets()
            payload = await self._ex.fetch_open_interest_history(
                self._ccxt_sym(sym), timeframe=period, limit=2
            )
            series = [float(item["openInterestAmount"]) for item in payload
                      if item.get("openInterestAmount") is not None]
            if len(series) < 2 or series[-2] <= 0:
                return None
            change = series[-1] / series[-2] - 1.0
            self._open_interest_change_cache[cache_key] = (now, change)
            return change
        except Exception as exc:
            LOG.warning("oi_change failed | symbol=%s period=%s error=%s", sym, period, exc)
        return None

    async def fetch_long_short_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Top trader long/short account ratio (topLongShortAccountRatio)."""
        return await self._fetch_fapi_metric(
            symbol,
            period=period,
            fetcher=self._ex.fapiDataGetTopLongShortAccountRatio,
            ratio_keys=("longShortRatio", "long_short_ratio"),
            cache=self._long_short_ratio_cache,
            ttl_key="long_short_ratio",
        )

    async def fetch_top_position_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Top trader long/short position ratio (topLongShortPositionRatio)."""
        return await self._fetch_fapi_metric(
            symbol,
            period=period,
            fetcher=self._ex.fapiDataGetTopLongShortPositionRatio,
            ratio_keys=("longShortRatio", "long_short_ratio"),
            cache=self._top_position_ls_ratio_cache,
            ttl_key="long_short_ratio",
        )

    async def fetch_global_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Global long/short account ratio (globalLongShortAccountRatio)."""
        return await self._fetch_fapi_metric(
            symbol,
            period=period,
            fetcher=self._ex.fapiDataGetGlobalLongShortAccountRatio,
            ratio_keys=("longShortRatio", "long_short_ratio"),
            cache=self._global_ls_ratio_cache,
            ttl_key="long_short_ratio",
        )

    async def fetch_taker_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Taker buy/sell volume ratio (takerlongshortRatio buySellRatio)."""
        if self._ccxt_has(self._ex, "fetchTakerBuySellVolume"):
            sym = self._bin_sym(symbol)
            cache_key = (sym, period)
            now = time.monotonic()
            cached = self._taker_ratio_cache.get(cache_key)
            if self._cache_fresh(cached, _CACHE_TTL["taker_ratio"]):
                return cached[1]  # type: ignore[index]
            try:
                await self.load_markets()
                rows = await self._ex.fetch_taker_buy_sell_volume(
                    self._ccxt_sym(sym),
                    timeframe=period,
                    limit=1,
                )
                if rows:
                    item = rows[-1]
                    buy = _safe_float(item.get("takerBuyQuoteVolume"))
                    sell = _safe_float(item.get("takerSellQuoteVolume"))
                    if buy > 0 and sell > 0:
                        value = buy / sell
                        self._taker_ratio_cache[cache_key] = (now, value)
                        return value
            except Exception as exc:
                LOG.warning("fetch_taker_buy_sell_volume failed | sym=%s error=%s", sym, exc)
            return None
        return await self._fetch_fapi_metric(
            symbol,
            period=period,
            fetcher=self._ex.fapiDataGetTakerlongshortRatio,
            ratio_keys=("buySellRatio", "buy_sell_ratio"),
            cache=self._taker_ratio_cache,
            ttl_key="taker_ratio",
        )

    async def fetch_open_interest_series(
        self, symbol: str, *, period: str = "5m", limit: int = 48
    ) -> list[float]:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period, int(limit))
        cached = self._oi_series_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["metric_series"]):
            return cached[1]  # type: ignore[index]
        try:
            await self.load_markets()
            payload = await self._ex.fetch_open_interest_history(
                self._ccxt_sym(sym), timeframe=period, limit=int(limit)
            )
            series = [float(item["openInterestAmount"]) for item in payload
                      if item.get("openInterestAmount") is not None]
            if series:
                self._oi_series_cache[cache_key] = (time.monotonic(), series)
            return series
        except Exception as exc:
            LOG.warning(
                "fetch_open_interest_history_series failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
            raise

    async def fetch_global_ls_series(
        self, symbol: str, *, period: str = "5m", limit: int = 48
    ) -> list[float]:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period, int(limit))
        cached = self._gls_series_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["metric_series"]):
            return cached[1]  # type: ignore[index]
        try:
            await self.load_markets()
            payload = await self._ex.fetch_long_short_ratio_history(
                self._ccxt_sym(sym), timeframe=period, limit=int(limit)
            )
            series = [float(item["longShortRatio"]) for item in payload
                      if item.get("longShortRatio") is not None]
            if series:
                self._gls_series_cache[cache_key] = (time.monotonic(), series)
            return series
        except Exception as exc:
            LOG.warning(
                "fetch_long_short_ratio_history failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
            raise

    async def fetch_funding_rate(self, symbol: str) -> float | None:
        sym = self._bin_sym(symbol)
        now = time.monotonic()
        cached = self._funding_rate_cache.get(sym)
        if self._cache_fresh(cached, _CACHE_TTL["funding_rate"]):
            return cached[1]  # type: ignore[index]
        await self.load_markets()
        try:
            payload = await self._ex.fetch_funding_rate(self._ccxt_sym(sym))
            value = payload.get("fundingRate")
            if value is not None:
                rate = float(value)
                self._funding_rate_cache[sym] = (now, rate)
                return rate
        except Exception as exc:
            LOG.warning("fetch_funding_rate failed | symbol=%s error=%s", sym, exc)
            raise
        return None

    async def fetch_funding_rate_history(
        self, symbol: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        sym = self._bin_sym(symbol)
        now = time.monotonic()
        cached = self._funding_history_cache.get(sym)
        if cached is not None and now - cached[0] < 900:
            return cached[1]
        await self.load_markets()
        try:
            payload = await self._ex.fetch_funding_rate_history(self._ccxt_sym(sym), limit=limit)
            rows: list[dict[str, Any]] = []
            for item in payload:
                rows.append(
                    {
                        "fundingTime": int(item.get("timestamp") or 0),
                        "fundingRate": float(item.get("fundingRate") or 0.0),
                        "markPrice": float(item.get("markPrice") or item.get("info", {}).get("markPrice") or 0.0),
                    }
                )
            rows.sort(key=lambda r: r["fundingTime"])
            self._funding_history_cache[sym] = (now, rows)
            return rows
        except Exception as exc:
            LOG.warning("fetch_funding_rate_history failed | symbol=%s error=%s", sym, exc)
            raise

    async def fetch_premium_index_all(self) -> dict[str, dict[str, float]]:
        now = time.monotonic()
        if self._cache_fresh(self._premium_index_all_cache, 30):
            assert self._premium_index_all_cache is not None
            return self._premium_index_all_cache[1]
        await self.load_markets()
        funding = await self._ex.fetch_funding_rates()
        rows: dict[str, dict[str, float]] = {}
        for ccxt_sym, item in funding.items():
            if not is_linear_usdt_swap_market(self._ex.markets.get(ccxt_sym)):
                continue
            resolved = try_binance_id_from_ccxt(ccxt_sym, exchange=self._ex)
            if not resolved:
                continue
            sym = self._bin_sym(resolved)
            mark = _safe_float(item.get("markPrice"))
            index = _safe_float(item.get("indexPrice"))
            if not sym or mark <= 0:
                continue
            rows[sym] = {
                "mark_price": mark,
                "index_price": index,
                "last_funding_rate": _safe_float(item.get("fundingRate")),
            }
        self._premium_index_all_cache = (now, rows)
        return rows

    async def fetch_funding_info_all(self) -> dict[str, dict[str, float | int]]:
        now = time.monotonic()
        if self._cache_fresh(self._funding_info_all_cache, _CACHE_TTL["funding_info"]):
            assert self._funding_info_all_cache is not None
            return self._funding_info_all_cache[1]
        await self.load_markets()
        intervals = await self._ex.fetch_funding_intervals()
        rows: dict[str, dict[str, float | int]] = {}
        for ccxt_sym, item in intervals.items():
            if not is_linear_usdt_swap_market(self._ex.markets.get(ccxt_sym)):
                continue
            info = item.get("info") if isinstance(item, dict) else None
            info = info if isinstance(info, dict) else {}
            resolved = try_binance_id_from_ccxt(ccxt_sym, exchange=self._ex)
            if not resolved:
                continue
            sym = self._bin_sym(resolved)
            rows[sym] = {
                "funding_interval_hours": int(info.get("fundingIntervalHours") or 8),
                "cap": _safe_float(info.get("adjustedFundingRateCap")),
                "floor": _safe_float(info.get("adjustedFundingRateFloor")),
            }
        self._funding_info_all_cache = (now, rows)
        return rows

    async def fetch_basis(self, symbol: str, *, period: str = "1h", limit: int = 3) -> float | None:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period)
        now = time.monotonic()
        cached = self._basis_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["basis"]):
            return cached[1]  # type: ignore[index]
        if sym in self._basis_api_unsupported:
            return await self._fetch_basis_fallback(symbol, period=period)
        try:
            payload = await self._ex.fapiDataGetBasis(
                {
                    "pair": sym,
                    "contractType": "PERPETUAL",
                    "period": period,
                    "limit": limit,
                }
            )
            basis_series: list[float] = []
            for row in payload if isinstance(payload, list) else []:
                futures_price = _safe_float(row.get("futuresPrice"))
                index_price = _safe_float(row.get("indexPrice"))
                if index_price <= 0:
                    continue
                basis_series.append((futures_price - index_price) / index_price * 100.0)
            if not basis_series:
                return await self._fetch_basis_fallback(symbol, period=period)
            s = pl.Series("basis", basis_series)
            basis_pct = float(s[-1])
            premium_slope = float(s[-1] - s[-2]) if len(basis_series) >= 2 else None
            premium_zscore = None
            if len(basis_series) >= 3:
                std = float(s.std(ddof=0) or 0.0)
                if std > 0:
                    premium_zscore = float((s[-1] - s.mean()) / std)
            self._basis_cache[cache_key] = (now, basis_pct)
            self._basis_stats_cache[cache_key] = (
                now,
                {
                    "latest_basis_pct": basis_pct,
                    "premium_slope_5m": premium_slope,
                    "premium_zscore_5m": premium_zscore,
                    "mark_index_spread_bps": basis_pct * 100.0,
                },
            )
            return basis_pct
        except Exception as exc:
            err = str(exc)
            if "-4104" in err or "Invalid contract type" in err:
                self._basis_api_unsupported.add(sym)
                LOG.debug(
                    "fetch_basis_perpetual_unsupported | symbol=%s period=%s",
                    sym,
                    period,
                )
                return await self._fetch_basis_fallback(symbol, period=period)
            LOG.debug(
                "fetch_basis_history failed | symbol=%s period=%s error=%s",
                sym,
                period,
                exc,
            )
            return None

    async def _fetch_basis_fallback(self, symbol: str, *, period: str) -> float | None:
        """Mark/index OHLCV basis for symbols without PERPETUAL fapiDataGetBasis."""
        stats = await self.fetch_basis_from_ohlcv(symbol, interval=period, limit=48)
        latest = stats.get("latest_basis_pct")
        return float(latest) if latest is not None else None

    async def fetch_agg_trade_snapshot(self, symbol: str, *, limit: int = 100) -> AggTradeSnapshot:
        sym = self._bin_sym(symbol)
        await self.load_markets()
        trades = await self._ex.fetch_trades(self._ccxt_sym(sym), limit=min(1000, max(1, limit)))
        buy_qty = sell_qty = 0.0
        for trade in trades:
            qty = _safe_float(trade.get("amount"))
            side = str(trade.get("side") or "").lower()
            if side == "buy":
                buy_qty += qty
            else:
                sell_qty += qty
        total = buy_qty + sell_qty
        delta_ratio = (buy_qty - sell_qty) / total if total > 0 else None
        return AggTradeSnapshot(
            symbol=sym,
            trade_count=len(trades),
            buy_qty=buy_qty,
            sell_qty=sell_qty,
            delta_ratio=delta_ratio,
        )

    def get_cached_open_interest(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        cached = self._open_interest_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_oi_change(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._open_interest_change_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._long_short_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_top_position_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._top_position_ls_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_global_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._global_ls_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_taker_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._taker_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    async def fetch_leverage_tiers(self, symbol: str) -> list[dict[str, Any]] | None:
        """Not available on public-only Hunt — Binance ``leverageBracket`` requires signed auth.

        Liquidation heatmap falls back to ``liquidation_heatmap._DEFAULT_LEVERAGE_TIERS``.
        """
        if not self._leverage_tiers_skip_logged:
            self._leverage_tiers_skip_logged = True
            LOG.info(
                "leverage_tiers_skipped_public_only | symbol=%s — "
                "CCXT fetchLeverageTiers needs apiKey; using default liq tiers",
                self._bin_sym(symbol),
            )
        return None

    def get_cached_leverage_tiers(
        self, symbol: str, *, max_age_s: float | None = None
    ) -> list[dict[str, Any]] | None:
        cached = self._leverage_tiers_cache.get(self._bin_sym(symbol))
        ttl = float(max_age_s if max_age_s is not None else _CACHE_TTL["leverage_tiers"])
        if not self._cache_fresh(cached, ttl):
            return None
        return cached[1]  # type: ignore[index]

    def get_cached_funding_rate(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        cached = self._funding_rate_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_funding_trend(self, symbol: str, max_age_s: float = 1800.0) -> str | None:
        cached = self._funding_history_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        rows = cached[1]
        if len(rows) < 3:
            return None
        s = pl.Series("r", [float(r["fundingRate"]) for r in rows[-4:]])
        diffs = s.diff().drop_nulls()
        ups = int((diffs > 0).sum())
        downs = int((diffs < 0).sum())
        steps = diffs.len()
        if ups >= steps * 0.75:
            return "rising"
        if downs >= steps * 0.75:
            return "falling"
        return "flat"

    def get_cached_funding_rate_zscore(
        self, symbol: str, *, max_cache_age_s: float = 1800.0
    ) -> float | None:
        cached = self._funding_history_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_cache_age_s:
            return None
        s = pl.Series("rates", [float(r["fundingRate"]) for r in cached[1]]).drop_nans()
        if s.len() < 6:
            return None
        stdev = float(s.std(ddof=1) or 0.0)
        if stdev <= 1e-12:
            return 0.0
        return float((s[-1] - s.mean()) / stdev)

    def get_cached_funding_recent_extreme(
        self,
        symbol: str,
        *,
        max_age_hours: float = 48.0,
        max_cache_age_s: float = 1800.0,
    ) -> tuple[float, float] | None:
        cached = self._funding_history_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_cache_age_s or not cached[1]:
            return None
        now_ms = int(time.time() * 1000)
        max_age_ms = max(0.0, float(max_age_hours)) * 3_600_000.0
        candidates: list[tuple[float, float]] = []
        for row in cached[1]:
            rate = float(row.get("fundingRate") or 0.0)
            funding_time = int(row.get("fundingTime") or 0)
            if funding_time <= 0:
                continue
            age_ms = max(0, now_ms - funding_time)
            if age_ms <= max_age_ms:
                candidates.append((rate, age_ms / 3_600_000.0))
        if not candidates:
            return None
        return max(candidates, key=lambda item: abs(item[0]))

    def get_cached_basis_stats(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> dict[str, float | None] | None:
        cached = self._basis_stats_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return dict(cached[1])

    def update_basis_from_websocket(
        self,
        symbol: str,
        mark_price: float,
        index_price: float | None = None,
        period: str = "5m",
    ) -> dict[str, float | None] | None:
        if index_price is None or index_price <= 0 or mark_price <= 0:
            return None
        basis_pct = (mark_price - index_price) / index_price * 100.0
        now = time.monotonic()
        cache_key = (self._bin_sym(symbol), period)
        prev = self._basis_stats_cache.get(cache_key)
        premium_slope = None
        if prev is not None:
            prev_basis = prev[1].get("latest_basis_pct")
            if prev_basis is not None:
                premium_slope = basis_pct - float(prev_basis)
        stats = {
            "latest_basis_pct": basis_pct,
            "premium_slope_5m": premium_slope,
            "premium_zscore_5m": prev[1].get("premium_zscore_5m") if prev else None,
            "mark_index_spread_bps": basis_pct * 100.0,
        }
        self._basis_cache[cache_key] = (now, basis_pct)
        self._basis_stats_cache[cache_key] = (now, stats)
        return stats

    async def fetch_mark_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 96
    ) -> pl.DataFrame:
        """Mark price OHLCV via ccxt fetch_mark_ohlcv."""
        await self.load_markets()
        from hunt_core.market.factory import ccxt_ohlcv_to_frame, finalize_kline_frame
        rows = await self._ex.fetch_mark_ohlcv(self._ccxt_sym(symbol), interval, limit=limit)
        return finalize_kline_frame(
            ccxt_ohlcv_to_frame(rows, interval, exchange=self._ex),
            interval,
            exchange=self._ex,
        )

    async def fetch_index_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 96
    ) -> pl.DataFrame:
        """Index price OHLCV via ccxt fetch_index_ohlcv."""
        await self.load_markets()
        from hunt_core.market.factory import ccxt_ohlcv_to_frame, finalize_kline_frame
        rows = await self._ex.fetch_index_ohlcv(self._ccxt_sym(symbol), interval, limit=limit)
        return finalize_kline_frame(
            ccxt_ohlcv_to_frame(rows, interval, exchange=self._ex),
            interval,
            exchange=self._ex,
        )

    async def fetch_premium_index_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 96
    ) -> pl.DataFrame:
        """Premium index (basis %) OHLCV via ccxt fetch_premium_index_ohlcv."""
        await self.load_markets()
        from hunt_core.market.factory import ccxt_ohlcv_to_frame, finalize_kline_frame
        rows = await self._ex.fetch_premium_index_ohlcv(self._ccxt_sym(symbol), interval, limit=limit)
        return finalize_kline_frame(
            ccxt_ohlcv_to_frame(rows, interval, exchange=self._ex),
            interval,
            exchange=self._ex,
        )

    async def fetch_basis_from_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 48
    ) -> dict[str, float | None]:
        """Basis stats computed from mark vs index OHLCV frames via polars."""
        try:
            mark_df, index_df = await asyncio.gather(
                self.fetch_mark_ohlcv(symbol, interval, limit=limit),
                self.fetch_index_ohlcv(symbol, interval, limit=limit),
            )
            if mark_df.is_empty() or index_df.is_empty():
                return {}
            joined = mark_df.select(["time", pl.col("close").alias("mark_close")]).join(
                index_df.select(["time", pl.col("close").alias("index_close")]),
                on="time",
                how="inner",
            )
            if joined.is_empty():
                return {}
            basis = (
                (pl.col("mark_close") - pl.col("index_close")) / pl.col("index_close") * 100.0
            )
            joined = joined.with_columns(basis.alias("basis_pct"))
            s = joined["basis_pct"]
            latest = float(s[-1])
            slope = float(s[-1] - s[-2]) if s.len() >= 2 else None
            std = float(s.std(ddof=1) or 0.0)
            zscore = float((s[-1] - s.mean()) / std) if std > 0 else None
            cache_key = (self._bin_sym(symbol), interval)
            now = time.monotonic()
            stats = {
                "latest_basis_pct": latest,
                "premium_slope_5m": slope,
                "premium_zscore_5m": zscore,
                "mark_index_spread_bps": latest * 100.0,
            }
            self._basis_cache[cache_key] = (now, latest)
            self._basis_stats_cache[cache_key] = (now, stats)
            return stats
        except Exception as exc:
            LOG.debug("fetch_basis_from_ohlcv failed | symbol=%s error=%s", symbol, exc)
            return {}

    # ── Secondary exchange REST (configurable via HUNT_CROSS_EXCHANGES) ──────

    async def _get_secondary(self, name: str) -> ccxt.Exchange | None:
        """Return cached secondary REST client, or None if init previously failed."""
        if name in self._secondary_failed:
            return None
        if name in self._secondary_clients:
            return self._secondary_clients[name]
        async with self._secondary_lock:
            if name in self._secondary_failed:
                return None
            if name in self._secondary_clients:
                return self._secondary_clients[name]
            ex_id = self._secondary_exchange_ids[name]
            ex: ccxt.Exchange = create_async_secondary_swap(
                ex_id,
                proxy_url=self._proxy_url,
                trust_env=self._trust_env,
                timeout_ms=self._timeout_ms,
            )
            try:
                await ex.load_markets()
                usdt_swap = sum(
                    1
                    for m in ex.markets.values()
                    if isinstance(m, dict)
                    and str(m.get("settle") or "").upper() == "USDT"
                    and str(m.get("type") or "") in {"swap", "future"}
                )
                if usdt_swap <= 0:
                    LOG.warning("secondary_rest_no_usdt_swap | exchange=%s", name)
                    await ex.close()
                    self._secondary_failed.add(name)
                    return None
            except Exception as exc:
                LOG.warning("secondary_load_markets_failed | exchange=%s error=%s", name, exc)
                await close_exchange_async(ex, label=f"secondary_rest_init:{name}")
                self._secondary_failed.add(name)
                return None
            self._secondary_clients[name] = ex
            return ex

    async def _secondary_ccxt_symbol(self, exchange_name: str, binance_sym: str) -> str | None:
        """Resolve Binance id on a secondary venue; None if not listed there."""
        ex = await self._get_secondary(exchange_name)
        if ex is None:
            return None
        try:
            return resolve_linear_usdt_swap(binance_sym, exchange=ex)
        except Exception as exc:
            LOG.debug(
                "secondary_symbol_not_listed | exchange=%s symbol=%s error=%s",
                exchange_name,
                binance_sym,
                exc,
            )
            return None

    async def _fetch_secondary_funding(
        self, name: str, ccxt_sym: str
    ) -> dict[str, float | None]:
        cache_key = (name, ccxt_sym)
        now = time.monotonic()
        cached = self._secondary_funding_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["secondary_funding"]):
            return cached[1]  # type: ignore[index]
        try:
            ex = await self._get_secondary(name)
            if ex is None:
                result: dict[str, float | None] = {"fundingRate": None}
            else:
                r = await ex.fetch_funding_rate(ccxt_sym)
                result = {"fundingRate": float(r.get("fundingRate") or 0)}
        except Exception as exc:
            LOG.warning(
                "secondary_funding_failed | exchange=%s sym=%s error=%s",
                name,
                ccxt_sym,
                exc,
            )
            result = {"fundingRate": None}
        self._secondary_funding_cache[cache_key] = (now, result)
        return result

    async def _fetch_secondary_oi(
        self, name: str, ccxt_sym: str
    ) -> dict[str, float | None]:
        cache_key = (name, ccxt_sym)
        now = time.monotonic()
        cached = self._secondary_oi_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["secondary_oi"]):
            return cached[1]  # type: ignore[index]
        try:
            ex = await self._get_secondary(name)
            if ex is None:
                result: dict[str, float | None] = {"oi_usd": None}
            else:
                r = await ex.fetch_open_interest(ccxt_sym)
                oi_val = (
                    float(r.get("openInterestValue") or r.get("openInterest") or 0) or None
                )
                result = {"oi_usd": oi_val}
        except Exception as exc:
            LOG.warning(
                "secondary_oi_failed | exchange=%s sym=%s error=%s",
                name,
                ccxt_sym,
                exc,
            )
            result = {"oi_usd": None}
        self._secondary_oi_cache[cache_key] = (now, result)
        return result

    async def _fetch_secondary_ticker(
        self, name: str, ccxt_sym: str
    ) -> dict[str, float | None]:
        try:
            ex = await self._get_secondary(name)
            if ex is None:
                return {"mark_price": None}
            t = await ex.fetch_ticker(ccxt_sym)
            mark = float(t.get("mark") or t.get("last") or 0) or None
            return {"mark_price": mark}
        except Exception as exc:
            LOG.warning(
                "secondary_ticker_failed | exchange=%s sym=%s error=%s",
                name,
                ccxt_sym,
                exc,
            )
            return {"mark_price": None}

    async def fetch_secondary_tickers(self, name: str) -> list[dict[str, float | str]]:
        """All linear-USDT-swap 24h tickers from one configured secondary venue.

        Rows are normalized to the same shape as :meth:`fetch_ticker_24h`
        (Binance-style ``symbol``/``last_price``/``quote_volume``...). Returns an
        empty list if the venue is unavailable — soft overlay, never fatal.
        """
        ex = await self._get_secondary(name)
        if ex is None:
            return []
        try:
            tickers = await ex.fetch_tickers()
        except Exception as exc:
            LOG.warning("secondary_tickers_failed | exchange=%s error=%s", name, exc)
            return []
        rows: list[dict[str, float | str]] = []
        for ccxt_sym, item in tickers.items():
            market = ex.markets.get(ccxt_sym) if isinstance(ex.markets, dict) else None
            if not is_linear_usdt_swap_market(market):
                continue
            base = str((market or {}).get("base") or "").upper()
            if not base:
                continue
            bin_sym = f"{base}USDT"
            last_price = _safe_float(item.get("last"))
            quote_volume = _safe_float(item.get("quoteVolume"))
            if last_price <= 0 or quote_volume <= 0:
                continue
            row: dict[str, float | str] = {
                "symbol": bin_sym,
                "exchange": name,
                "last_price": last_price,
                "price_change_percent": _safe_float(item.get("percentage")),
                "quote_volume": quote_volume,
            }
            high = _safe_float(item.get("high"))
            low = _safe_float(item.get("low"))
            if high > 0:
                row["high_price"] = high
            if low > 0:
                row["low_price"] = low
            rows.append(row)
        return rows

    async def fetch_cross_exchange_snapshot(self, symbol: str) -> dict[str, Any]:
        """
        Fetch funding / OI / mark-price for symbol from Bybit + OKX + Bitget in parallel.

        Returns a dict with:
          funding:   {exchange: rate|None}
          oi_usd:    {exchange: value|None}
          mark_price:{exchange: price|None}
          funding_spread: abs(max−min) across exchanges with data
          funding_consensus: "bull"|"bear"|"neutral"|"divergent"
          oi_total:  sum of OI across exchanges with data
          price_divergence_pct: max price spread / mean (%)
        """
        bin_sym = self._bin_sym(symbol)
        await self.load_markets()
        premium_all = await self.fetch_premium_index_all()
        pr = premium_all.get(bin_sym) or {}
        ref_funding = float(pr.get("last_funding_rate") or 0)
        ref_mark = float(pr.get("mark_price") or 0)

        listed: dict[str, bool] = {"binance": True}
        funding: dict[str, float | None] = {"binance": ref_funding or None}
        oi_usd: dict[str, float | None] = {}
        mark_price: dict[str, float | None] = {"binance": ref_mark or None}

        async def _fetch_one_secondary(name: str) -> tuple[str, str | None, Any]:
            ccxt_sym = await self._secondary_ccxt_symbol(name, bin_sym)
            listed[name] = ccxt_sym is not None
            if ccxt_sym is None:
                return name, None, None
            res = await asyncio.gather(
                self._fetch_secondary_funding(name, ccxt_sym),
                self._fetch_secondary_oi(name, ccxt_sym),
                self._fetch_secondary_ticker(name, ccxt_sym),
            )
            return name, ccxt_sym, res

        secondary_results = await asyncio.gather(
            *(_fetch_one_secondary(name) for name in self._secondary_exchange_ids),
            return_exceptions=True,
        )

        for item in secondary_results:
            if isinstance(item, Exception):
                LOG.warning("cross_exchange_secondary_batch_failed | error=%s", item)
                continue
            name, _ccxt_sym, res = item
            if res is None:
                funding[name] = None
                oi_usd[name] = None
                mark_price[name] = None
                continue
            f_r, oi_r, t_r = res
            funding[name] = f_r.get("fundingRate")
            oi_usd[name] = oi_r.get("oi_usd")
            mark_price[name] = t_r.get("mark_price")

        # Aggregate
        rates = [v for v in funding.values() if v is not None]
        funding_spread = round(max(rates) - min(rates), 6) if len(rates) >= 2 else 0.0

        consensus: str
        if len(rates) < 2:
            consensus = "neutral"
        elif all(r > 0.0001 for r in rates):
            consensus = "bull"
        elif all(r < -0.0001 for r in rates):
            consensus = "bear"
        elif funding_spread > 0.0005:
            consensus = "divergent"
        else:
            consensus = "neutral"

        oi_values = [v for v in oi_usd.values() if v is not None]
        oi_total = round(sum(oi_values), 0) if oi_values else 0.0

        prices = [v for v in mark_price.values() if v and v > 0]
        price_div = 0.0
        if len(prices) >= 2:
            mean_p = sum(prices) / len(prices)
            price_div = round((max(prices) - min(prices)) / mean_p * 100, 4) if mean_p > 0 else 0.0

        return {
            "symbol": bin_sym,
            "funding": funding,
            "oi_usd": oi_usd,
            "mark_price": mark_price,
            "listed": listed,
            "funding_spread": funding_spread,
            "funding_consensus": consensus,
            "oi_total": oi_total,
            "price_divergence_pct": price_div,
        }


# --- merged from market/book_parsers.py ---

from typing import Any


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def depth_imbalance_from_levels(
    bids: list[Any] | tuple[Any, ...] | None,
    asks: list[Any] | tuple[Any, ...] | None,
    *,
    top_n: int = 20,
) -> float | None:
    """Depth imbalance from top-N book levels (qty sum, not notional)."""
    bid_qty = 0.0
    ask_qty = 0.0
    for row in (bids or [])[:top_n]:
        try:
            bid_qty += float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
    for row in (asks or [])[:top_n]:
        try:
            ask_qty += float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
    return depth_imbalance_from_book(bid_qty=bid_qty, ask_qty=ask_qty, delta_ratio=None)


def depth_imbalance_from_book(
    *, bid_qty: float | None, ask_qty: float | None, delta_ratio: float | None
) -> float | None:
    """Return top-of-book depth imbalance, falling back to signed trade flow."""
    if bid_qty is not None and ask_qty is not None and (bid_qty >= 0) and (ask_qty >= 0):
        total = bid_qty + ask_qty
        if total > 0.0:
            return round(_clamp((bid_qty - ask_qty) / total), 4)
    if delta_ratio is None:
        return None
    return round(_clamp(float(delta_ratio)), 4)


def microprice_bias_from_book(
    *,
    bid: float | None,
    ask: float | None,
    bid_qty: float | None = None,
    ask_qty: float | None = None,
    delta_ratio: float | None,
) -> float | None:
    """Return signed microprice bias from L1 book, falling back to trade flow."""
    if bid is None or ask is None or bid <= 0 or (ask <= 0):
        return None
    spread = ask - bid
    mid = (bid + ask) / 2.0
    if mid <= 0 or spread <= 0:
        return None
    if bid_qty is not None and ask_qty is not None and (bid_qty >= 0) and (ask_qty >= 0):
        total_qty = bid_qty + ask_qty
        if total_qty > 0.0:
            microprice = (ask * bid_qty + bid * ask_qty) / total_qty
            half_spread = spread / 2.0
            if half_spread > 0.0:
                return round(_clamp((microprice - mid) / half_spread), 4)
    if delta_ratio is None:
        return None
    return round(_clamp(float(delta_ratio)), 4)

# --- merged from market/depth_walls.py ---

from dataclasses import dataclass
from typing import Any

_TOP_BOOK_WALL_LEVELS = 5


@dataclass(frozen=True, slots=True)
class WallCluster:
    price_center: float
    total_notional: float
    significance_pct: float
    level_count: int
    side: str
    distance_pct: float
    book_depth_pctile: float = 0.0


def _book_depth_percentile(notional: float, book_notionals: list[float]) -> float:
    """Relative significance as percentile rank within visible book depth."""
    if notional <= 0 or not book_notionals:
        return 0.0
    pool = sorted(n for n in book_notionals if n > 0)
    if not pool:
        return 0.0
    below = sum(1 for n in pool if n <= notional)
    return round(100.0 * below / len(pool), 1)


def detect_wall_clusters(
    levels: list[tuple[float, float]],
    *,
    current_price: float,
    daily_volume: float,
    side: str,
    cluster_tolerance_pct: float = 0.3,
    min_significance_pct: float = 0.5,
    min_book_depth_pctile: float = 85.0,
) -> list[WallCluster]:
    """Group adjacent book levels into wall clusters ranked by distance from price."""
    if current_price <= 0 or not levels:
        return []
    tol = current_price * cluster_tolerance_pct / 100.0
    sorted_levels = sorted(
        ((float(p), float(q)) for p, q in levels if float(p) > 0 and float(q) > 0),
        key=lambda x: x[0],
    )
    level_notionals = [p * q for p, q in sorted_levels]
    clusters: list[WallCluster] = []
    group: list[tuple[float, float]] = []
    anchor = 0.0

    def _flush() -> None:
        if not group:
            return
        total = sum(p * q for p, q in group)
        qty_sum = sum(q for _p, q in group)
        center = sum(p * q for p, q in group) / max(qty_sum, 1e-12)
        sig = (total / daily_volume * 100.0) if daily_volume > 0 else 0.0
        depth_pctile = _book_depth_percentile(total, level_notionals)
        dist = abs(center - current_price) / current_price * 100.0
        if sig >= min_significance_pct or depth_pctile >= min_book_depth_pctile:
            clusters.append(
                WallCluster(
                    price_center=round(center, 6),
                    total_notional=round(total, 2),
                    significance_pct=round(sig, 3),
                    level_count=len(group),
                    side=side,
                    distance_pct=round(dist, 3),
                    book_depth_pctile=depth_pctile,
                )
            )

    for price, qty in sorted_levels:
        if not group:
            group = [(price, qty)]
            anchor = price
            continue
        if abs(price - anchor) <= tol:
            group.append((price, qty))
        else:
            _flush()
            group = [(price, qty)]
            anchor = price
    _flush()
    return sorted(clusters, key=lambda c: c.distance_pct)


def depth_imbalance_by_zone(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    current_price: float,
    zones_pct: list[float] | None = None,
) -> dict[str, float]:
    """Imbalance (-1..1) within each distance band from mid."""
    if current_price <= 0:
        return {}
    zones = zones_pct or [0.5, 1.0, 2.0, 5.0]
    out: dict[str, float] = {}
    for z in zones:
        band = current_price * z / 100.0
        lo = current_price - band
        hi = current_price + band
        bid_n = sum(p * q for p, q in bids if lo <= p <= current_price)
        ask_n = sum(p * q for p, q in asks if current_price <= p <= hi)
        total = bid_n + ask_n
        key = f"imb_{z:g}pct"
        out[key] = round((bid_n - ask_n) / total, 4) if total > 0 else 0.0
    return out


def top_depth_walls(
    levels: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    *,
    top_n: int = _TOP_BOOK_WALL_LEVELS,
) -> list[dict[str, float]]:
    """Top bid/ask levels ranked by notional (price × qty)."""
    ranked = sorted(
        (
            {
                "price": float(price),
                "qty": float(qty),
                "notional_usd": round(float(price) * float(qty), 2),
            }
            for price, qty in levels
            if float(price) > 0 and float(qty) > 0
        ),
        key=lambda row: row["notional_usd"],
        reverse=True,
    )
    return ranked[: max(1, int(top_n))]


def normalize_depth_levels(raw: Any) -> list[tuple[float, float]]:
    """Accept ccxt [[p,q],…] or list of {price, qty} dicts."""
    if not isinstance(raw, list):
        return []
    out: list[tuple[float, float]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((float(item[0]), float(item[1])))
        elif isinstance(item, dict):
            p = item.get("price")
            q = item.get("qty")
            if p is not None and q is not None:
                out.append((float(p), float(q)))
    return out


def depth_snapshot_from_book(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    top_n: int = _TOP_BOOK_WALL_LEVELS,
) -> dict[str, Any]:
    """Build hunt depth snapshot with ranked walls."""
    if not bids or not asks:
        return {
            "bid_price": None,
            "ask_price": None,
            "bid_qty": None,
            "ask_qty": None,
            "bid_levels": [],
            "ask_levels": [],
        }
    return {
        "bid_price": float(bids[0][0]),
        "ask_price": float(asks[0][0]),
        "bid_qty": round(sum(q for _p, q in bids), 4),
        "ask_qty": round(sum(q for _p, q in asks), 4),
        "bid_levels": top_depth_walls(bids, top_n=top_n),
        "ask_levels": top_depth_walls(asks, top_n=top_n),
    }


def aggregate_cross_exchange_walls(
    per_exchange: dict[str, dict[str, Any]],
    *,
    top_n: int = _TOP_BOOK_WALL_LEVELS,
) -> dict[str, Any]:
    """Merge venue depth snapshots — rank walls globally by notional."""
    bid_pool: list[dict[str, Any]] = []
    ask_pool: list[dict[str, Any]] = []
    venues: list[str] = []
    for ex, snap in per_exchange.items():
        if not isinstance(snap, dict) or snap.get("bid_price") is None:
            continue
        venues.append(ex)
        for side, pool in (("bid", bid_pool), ("ask", ask_pool)):
            key = f"{side}_levels"
            for lvl in snap.get(key) or []:
                if isinstance(lvl, dict):
                    row = dict(lvl)
                elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    row = {
                        "price": float(lvl[0]),
                        "qty": float(lvl[1]),
                        "notional_usd": round(float(lvl[0]) * float(lvl[1]), 2),
                    }
                else:
                    continue
                row["exchange"] = ex
                pool.append(row)

    def _top(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(pool, key=lambda r: float(r.get("notional_usd") or 0), reverse=True)[
            :top_n
        ]

    total_bid = sum(
        float(lvl.get("notional_usd") or 0)
        for snap in per_exchange.values()
        if isinstance(snap, dict)
        for lvl in (snap.get("bid_levels") or [])
        if isinstance(lvl, dict)
    )
    total_ask = sum(
        float(lvl.get("notional_usd") or 0)
        for snap in per_exchange.values()
        if isinstance(snap, dict)
        for lvl in (snap.get("ask_levels") or [])
        if isinstance(lvl, dict)
    )
    imb = None
    if total_bid + total_ask > 0:
        imb = round((total_bid - total_ask) / (total_bid + total_ask), 4)

    return {
        "venues": venues,
        "bid_levels": _top(bid_pool),
        "ask_levels": _top(ask_pool),
        "depth_imbalance": imb,
        "bid_depth_usd_total": round(total_bid, 2),
        "ask_depth_usd_total": round(total_ask, 2),
        "source": "cross_exchange",
    }


def wall_cluster_to_dict(cluster: WallCluster) -> dict[str, Any]:
    """Serialize a wall cluster for market/snapshot payloads."""
    return {
        "price_center": cluster.price_center,
        "total_notional": cluster.total_notional,
        "significance_pct": cluster.significance_pct,
        "level_count": cluster.level_count,
        "side": cluster.side,
        "distance_pct": cluster.distance_pct,
        "book_depth_pctile": cluster.book_depth_pctile,
    }




# --- merged from market/liquidation_heatmap.py ---

import collections
import time
from dataclasses import dataclass
from typing import Any

_DEFAULT_LEVERAGE_TIERS = (5, 10, 20, 50)


def maintenance_rates_from_tiers(tiers: list[dict[str, Any]]) -> tuple[float, ...]:
    """Extract unique maintenance margin rates from Binance/CCXT bracket rows."""
    rates: list[float] = []
    seen: set[float] = set()
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        mmr = tier.get("maintenance_margin_rate")
        if mmr is None:
            mmr = tier.get("maintenanceMarginRate")
        try:
            val = float(mmr)
        except (TypeError, ValueError):
            continue
        if val <= 0 or val >= 1 or val in seen:
            continue
        seen.add(val)
        rates.append(val)
    return tuple(sorted(rates))


def leverage_tiers_from_brackets(tiers: list[dict[str, Any]]) -> tuple[int, ...]:
    """Fallback leverage integers from bracket max_leverage when MMR rows are absent."""
    levs: list[int] = []
    seen: set[int] = set()
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        raw = tier.get("max_leverage")
        if raw is None:
            raw = tier.get("maxLeverage")
        try:
            lev = int(raw)
        except (TypeError, ValueError):
            continue
        if lev <= 0 or lev in seen:
            continue
        seen.add(lev)
        levs.append(lev)
    if not levs:
        return _DEFAULT_LEVERAGE_TIERS
    return tuple(sorted(levs, reverse=True)[:8])


@dataclass(frozen=True, slots=True)
class LiquidationDensityZone:
    price_lo: float
    price_hi: float
    price_center: float
    total_notional: float
    long_notional: float
    short_notional: float
    intensity: float
    event_count: int
    side_bias: str | None


@dataclass(frozen=True, slots=True)
class LiquidationCluster:
    price: float
    total_notional: float
    long_notional: float
    short_notional: float
    event_count: int
    intensity: float


@dataclass(frozen=True, slots=True)
class LiquidationHeatmap:
    clusters: tuple[LiquidationCluster, ...]
    density_zones: tuple[LiquidationDensityZone, ...]
    nearest_long_liquidation: float | None
    nearest_short_liquidation: float | None
    cascade_risk_direction: str | None
    total_long_at_risk: float
    total_short_at_risk: float


def _bucket_events(
    buffer: collections.deque[tuple[int, str, str, float, float]],
    *,
    symbol: str,
    current_price: float,
    window_seconds: int,
    n_buckets: int,
    price_range_pct: float,
) -> dict[int, dict[str, float]]:
    if current_price <= 0:
        return {}
    cutoff_ms = int(time.time() * 1000) - window_seconds * 1000
    span = current_price * price_range_pct / 100.0
    price_min = current_price - span
    price_max = current_price + span
    bucket_size = (price_max - price_min) / max(1, n_buckets)
    buckets: dict[int, dict[str, float]] = {}
    for ts_ms, sym, side, qty, price in buffer:
        if ts_ms < cutoff_ms or sym != symbol:
            continue
        try:
            qty_val = float(qty)
            price_val = float(price)
        except (TypeError, ValueError):
            continue
        if qty_val <= 0 or price_val <= 0:
            continue
        if price_val < price_min or price_val > price_max:
            continue
        b = int((price_val - price_min) / bucket_size)
        b = max(0, min(n_buckets - 1, b))
        row = buckets.setdefault(
            b, {"long": 0.0, "short": 0.0, "total": 0.0, "events": 0.0}
        )
        notional = qty_val * price_val
        row["total"] += notional
        row["events"] += 1.0
        if side == "BUY":
            row["short"] += notional
        else:
            row["long"] += notional
    return buckets


def _prospective_levels(
    current_price: float,
    *,
    n_buckets: int,
    price_range_pct: float,
    leverage_tiers: tuple[int, ...] = _DEFAULT_LEVERAGE_TIERS,
    maintenance_margin_rates: tuple[float, ...] | None = None,
) -> list[tuple[float, float, str]]:
    """Estimate liquidation magnets from leverage brackets or generic tiers."""
    if current_price <= 0:
        return []
    out: list[tuple[float, float, str]] = []
    if maintenance_margin_rates:
        for mmr in maintenance_margin_rates:
            if mmr <= 0 or mmr >= 1:
                continue
            long_liq = current_price * (1.0 - mmr)
            short_liq = current_price * (1.0 + mmr)
            out.append((long_liq, mmr, "long"))
            out.append((short_liq, mmr, "short"))
    else:
        for lev in leverage_tiers:
            if lev <= 0:
                continue
            long_liq = current_price * (1.0 - 1.0 / lev)
            short_liq = current_price * (1.0 + 1.0 / lev)
            out.append((long_liq, 1.0 / lev, "long"))
            out.append((short_liq, 1.0 / lev, "short"))
    span = current_price * price_range_pct / 100.0
    lo = current_price - span
    hi = current_price + span
    return [(p, w, side) for p, w, side in out if lo <= p <= hi]


def build_liquidation_heatmap(
    buffer: collections.deque[tuple[int, str, str, float, float]],
    *,
    symbol: str,
    current_price: float,
    window_seconds: int = 300,
    n_buckets: int = 20,
    price_range_pct: float = 5.0,
    leverage_tiers: tuple[int, ...] | None = None,
    maintenance_margin_rates: tuple[float, ...] | None = None,
    bracket_tiers: list[dict[str, Any]] | None = None,
) -> LiquidationHeatmap | None:
    """Bucket WS liquidations ±price_range_pct; merge with prospective leverage levels."""
    if current_price <= 0:
        return None
    mm_rates = maintenance_margin_rates
    lev_tiers = leverage_tiers
    if bracket_tiers:
        parsed_mmr = maintenance_rates_from_tiers(bracket_tiers)
        if parsed_mmr:
            mm_rates = parsed_mmr
        elif lev_tiers is None:
            lev_tiers = leverage_tiers_from_brackets(bracket_tiers)
    if lev_tiers is None and not mm_rates:
        lev_tiers = _DEFAULT_LEVERAGE_TIERS
    span = current_price * price_range_pct / 100.0
    price_min = current_price - span
    bucket_size = (2.0 * span) / max(1, n_buckets)
    raw = _bucket_events(
        buffer,
        symbol=symbol,
        current_price=current_price,
        window_seconds=window_seconds,
        n_buckets=n_buckets,
        price_range_pct=price_range_pct,
    )
    cluster_map: dict[int, dict[str, float]] = {
        b: {"long": v["long"], "short": v["short"], "total": v["total"], "events": v["events"]}
        for b, v in raw.items()
    }
    for price, weight, side in _prospective_levels(
        current_price,
        n_buckets=n_buckets,
        price_range_pct=price_range_pct,
        leverage_tiers=lev_tiers or _DEFAULT_LEVERAGE_TIERS,
        maintenance_margin_rates=mm_rates,
    ):
        b = int((price - price_min) / bucket_size)
        b = max(0, min(n_buckets - 1, b))
        row = cluster_map.setdefault(b, {"long": 0.0, "short": 0.0, "total": 0.0, "events": 0.0})
        synthetic = current_price * weight * 0.05
        row["total"] += synthetic
        if side == "long":
            row["long"] += synthetic
        else:
            row["short"] += synthetic

    if not cluster_map:
        return None

    max_total = max(row["total"] for row in cluster_map.values()) or 1.0
    clusters: list[LiquidationCluster] = []
    for b, row in cluster_map.items():
        center = price_min + (b + 0.5) * bucket_size
        clusters.append(
            LiquidationCluster(
                price=round(center, 6),
                total_notional=round(row["total"], 2),
                long_notional=round(row["long"], 2),
                short_notional=round(row["short"], 2),
                event_count=int(row["events"]),
                intensity=round(row["total"] / max_total, 4),
            )
        )
    clusters.sort(key=lambda c: c.total_notional, reverse=True)
    top = tuple(clusters[:3])

    density_zones: list[LiquidationDensityZone] = []
    for b, row in sorted(cluster_map.items(), key=lambda kv: kv[1]["total"], reverse=True):
        lo = price_min + b * bucket_size
        hi = lo + bucket_size
        center = price_min + (b + 0.5) * bucket_size
        long_n = row["long"]
        short_n = row["short"]
        total = row["total"]
        intensity = round(total / max_total, 4)
        if intensity < 0.15 and row["events"] <= 0:
            continue
        if long_n > short_n * 1.3:
            bias: str | None = "long_liq"
        elif short_n > long_n * 1.3:
            bias = "short_liq"
        else:
            bias = None
        density_zones.append(
            LiquidationDensityZone(
                price_lo=round(lo, 6),
                price_hi=round(hi, 6),
                price_center=round(center, 6),
                total_notional=round(total, 2),
                long_notional=round(long_n, 2),
                short_notional=round(short_n, 2),
                intensity=intensity,
                event_count=int(row["events"]),
                side_bias=bias,
            )
        )
    zones_top = tuple(density_zones[:8])

    nearest_long: float | None = None
    nearest_short: float | None = None
    total_long_risk = 0.0
    total_short_risk = 0.0
    for c in clusters:
        if c.price < current_price:
            total_long_risk += c.long_notional
            if nearest_long is None or c.price > nearest_long:
                nearest_long = c.price
        elif c.price > current_price:
            total_short_risk += c.short_notional
            if nearest_short is None or c.price < nearest_short:
                nearest_short = c.price

    cascade: str | None = None
    if total_long_risk > total_short_risk * 1.5 and total_long_risk >= 25_000:
        cascade = "long_flush"
    elif total_short_risk > total_long_risk * 1.5 and total_short_risk >= 25_000:
        cascade = "short_squeeze"

    return LiquidationHeatmap(
        clusters=top,
        density_zones=zones_top,
        nearest_long_liquidation=nearest_long,
        nearest_short_liquidation=nearest_short,
        cascade_risk_direction=cascade,
        total_long_at_risk=round(total_long_risk, 2),
        total_short_at_risk=round(total_short_risk, 2),
    )


def heatmap_to_market_dict(
    heatmap: LiquidationHeatmap | None,
    *,
    prospective_source: str | None = None,
) -> dict[str, Any]:
    if heatmap is None:
        return {}
    out: dict[str, Any] = {
        "liq_heatmap_nearest_long": heatmap.nearest_long_liquidation,
        "liq_heatmap_nearest_short": heatmap.nearest_short_liquidation,
        "liq_cascade_risk": heatmap.cascade_risk_direction,
        "liq_long_at_risk_usd": heatmap.total_long_at_risk,
        "liq_short_at_risk_usd": heatmap.total_short_at_risk,
        "liq_heatmap_clusters": [
            {
                "price": c.price,
                "total_notional": c.total_notional,
                "intensity": c.intensity,
                "event_count": c.event_count,
            }
            for c in heatmap.clusters
        ],
        "liq_density_zones": [
            {
                "price_lo": z.price_lo,
                "price_hi": z.price_hi,
                "price_center": z.price_center,
                "total_notional": z.total_notional,
                "intensity": z.intensity,
                "side_bias": z.side_bias,
                "event_count": z.event_count,
            }
            for z in heatmap.density_zones
        ],
    }
    if prospective_source:
        out["liq_prospective_source"] = prospective_source
    return out



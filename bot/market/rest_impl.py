"""Binance USD-M public REST client (v9 split modules)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Set, Tuple, cast
from urllib.parse import urlparse

import aiohttp
import polars as pl

from bot.domain.schemas import (
    AggTrade,
    AggTradeSnapshot,
    SymbolFrames,
    SymbolMeta,
)

from bot.market.rate_limit import (
    _SlidingWindowRateLimiter,
    _WeightBudgetManager,
)
from bot.market.data import (
    MarketDataUnavailable,
    UTC,
    _REST_WEIGHT_SOFT_LIMIT,
    _REST_WEIGHT_HARD_LIMIT,
    _FAPI_BASE_URL,
    FORBIDDEN_PARAMS,
    _FORBIDDEN_PARAMS_LOWER,
    _REST_GLOBAL_SEMAPHORE,
    _FUTURES_DATA_IP_LIMIT_WINDOW_S,
    _FUTURES_DATA_IP_LIMIT_OFFICIAL_MAX,
    _FUTURES_DATA_IP_LIMIT_DEFAULT,
    _HTTP_CONNECTOR_LIMIT,
    _CACHE_TTL,
    _PERIOD_WINDOW_SECONDS,
    _KLINE_COLUMNS,
    _KLINE_FRAME_SCHEMA,
    _ENDPOINT_WEIGHTS,
    _FUTURES_DATA_REQUEST_LIMITED_OPS,
    _DEFAULT_KLINE_FETCH_LIMIT,
    _DEFAULT_ORDER_BOOK_DEPTH_LIMIT,
    _VALID_ORDER_BOOK_DEPTH_LIMITS,
    _FALLBACK_TIMEOUT_DEBUG_OPERATIONS,
    _PublicEndpointSpec,
    _PUBLIC_ENDPOINT_REGISTRY,
    _ALLOWED_PUBLIC_REST_PATHS,
    _FORBIDDEN_PUBLIC_PATH_MARKERS,
    _VALID_INTERVALS,
)
from bot.market.rest_validators import (
    validate_interval,
    validate_limit,
    validate_order_book_depth_limit,
    validate_runtime_public_rest_url,
    validate_symbol,
    _validate_rest_params,
)
from bot.market.rest_frames import (
    _coerce_rest_row,
    _drop_incomplete_ohlcv_tail,
    _klines_to_frame,
    _ohlcv_frame_has_incomplete_tail,
    _parse_depth_levels,
    _safe_float,
    _timeframe_to_seconds,
    _unwrap_model,
)

LOG = logging.getLogger("bot.market.rest")


from bot.market.rest_abc import BinanceClient
from bot.market.rest_http import RestHttpMixin


class BinanceClientImpl(RestHttpMixin, BinanceClient):
    """Production implementation of Binance public REST client."""

    def __init__(
        self,
        *,
        ws_manager: Any = None,  # FuturesWSManager | None
        rest_timeout_seconds: float = 20.0,
        futures_data_request_limit_per_5m: int = _FUTURES_DATA_IP_LIMIT_DEFAULT,
    ) -> None:
        self._rest_timeout = rest_timeout_seconds
        self._futures_data_limit_per_5m = max(
            30,
            min(
                int(futures_data_request_limit_per_5m),
                _FUTURES_DATA_IP_LIMIT_OFFICIAL_MAX,
            ),
        )
        self.client: Any = None
        self._exchange_info_cache: Tuple[float, List[SymbolMeta]] | None = None
        self._ticker_24h_cache: Tuple[float, List[Dict[str, float | str]]] | None = None
        self._premium_index_all_cache: Tuple[float, Dict[str, Dict[str, float]]] | None = None
        self._funding_rate_cache: Dict[str, Tuple[float, float]] = {}
        self._open_interest_cache: Dict[str, Tuple[float, float]] = {}
        self._open_interest_change_cache: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._long_short_ratio_cache: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._taker_ratio_cache: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._global_ls_ratio_cache: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._top_position_ls_ratio_cache: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._funding_history_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._basis_cache: Dict[Tuple[str, str], Tuple[float, float | None]] = {}
        self._basis_stats_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, float | None]]] = {}
        self._basis_ws_history: Dict[Tuple[str, str], Deque[Tuple[float, float]]] = {}
        self._order_book_depth_cache: Dict[
            Tuple[str, int], Tuple[float, Dict[str, float | None]]
        ] = {}
        self._ws: Any = ws_manager
        self._last_rest_weight_1m: int | None = None
        self._last_rest_response_time_ms: float | None = None
        self._rate_limit_pause_until = 0.0
        self._futures_data_pause_until = 0.0
        self._rate_limit_error_streak = 0
        self._weight_window_weight: int = 0
        self._weight_window_start: float = 0.0
        self._weight_budget = _WeightBudgetManager(
            max_weight=_REST_WEIGHT_SOFT_LIMIT,
            window_seconds=60.0,
        )
        self._futures_data_limiter = _SlidingWindowRateLimiter(
            max_requests=self._futures_data_limit_per_5m,
            window_seconds=_FUTURES_DATA_IP_LIMIT_WINDOW_S,
        )
        self._http_session: aiohttp.ClientSession | None = None
        self._klines_cache: Dict[Tuple[str, str, int], Tuple[float, Any]] = {}
        self._klines_locks: Dict[Tuple[str, str, int], asyncio.Lock] = {}
        self._derived_klines_cache: Dict[
            Tuple[str, str, str, int], Tuple[float, Any]
        ] = {}
        self._derived_klines_locks: Dict[Tuple[str, str, str, int], asyncio.Lock] = {}
        self._circuit_failures: Dict[str, int] = {}
        self._circuit_open_until: Dict[str, float] = {}
        self._circuit_half_open: Set[str] = set()
        self._circuit_failure_threshold = 3
        self._circuit_open_duration_seconds = 30.0
        self._critical_operations = {
            "kline_candlestick_data",
            "symbol_order_book_ticker",
            "order_book_depth",
            "exchange_information"
        }
        self._last_endpoint_name: str | None = None
        self._last_endpoint_source: str | None = None
        self._last_endpoint_cache_hit: bool = False
        self._last_endpoint_fallback_used: bool = False
        self._last_endpoint_limiter_wait_ms: float = 0.0
        self._last_endpoint_response_age_s: float | None = None

    # Implementation of all abstract methods follows...
    # For brevity, I'll show a few key implementations and note that the rest
    # would follow the same pattern from the original market_data.py

    async def fetch_exchange_symbols(self) -> List[SymbolMeta]:
        now = time.monotonic()
        if self._exchange_info_cache is not None:
            cached_at, rows = self._exchange_info_cache
            if now - cached_at < 3600:
                self._record_endpoint_snapshot(
                    "exchange_information",
                    source="rest",
                    cache_hit=True,
                    fallback_used=False,
                    response_age_s=now - cached_at,
                )
                return rows

        try:
            payload = await self._call_public_http_json("exchange_information")
        except MarketDataUnavailable as exc:
            if self._exchange_info_cache is not None:
                cached_at, rows = self._exchange_info_cache
                self._record_endpoint_snapshot(
                    "exchange_information",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=now - cached_at,
                )
                LOG.info(
                    "fetch_exchange_symbols failed, using stale cache | age=%.0fs error=%s",
                    now - cached_at,
                    exc.detail,
                )
                return rows
            raise
        symbols = (
            payload.get("symbols", [])
            if isinstance(payload, dict)
            else getattr(payload, "symbols", [])
        )
        rows = [
            SymbolMeta(
                symbol=str(item.get("symbol", ""))
                if isinstance(item, dict)
                else str(getattr(item, "symbol", "")),
                base_asset=str(item.get("baseAsset", ""))
                if isinstance(item, dict)
                else str(getattr(item, "base_asset", "")),
                quote_asset=str(item.get("quoteAsset", ""))
                if isinstance(item, dict)
                else str(getattr(item, "quote_asset", "")),
                contract_type=str(item.get("contractType", ""))
                if isinstance(item, dict)
                else str(getattr(item, "contract_type", "")),
                status=str(item.get("status", ""))
                if isinstance(item, dict)
                else str(getattr(item, "status", "")),
                onboard_date_ms=int(item.get("onboardDate", 0) or 0)
                if isinstance(item, dict)
                else int(getattr(item, "onboard_date", 0) or 0),
            )
            for item in symbols
        ]
        self._exchange_info_cache = (now, rows)
        return rows

    async def fetch_ticker_24h(self) -> List[Dict[str, float | str]]:
        now = time.monotonic()
        if self._ticker_24h_cache is not None:
            cached_at, rows = self._ticker_24h_cache
            if now - cached_at < 300:  # 5 min cache
                self._record_endpoint_snapshot(
                    "ticker24hr_price_change_statistics",
                    source="rest",
                    cache_hit=True,
                    fallback_used=False,
                    response_age_s=now - cached_at,
                )
                return rows

        try:
            payload = await self._call_public_http_json(
                "ticker24hr_price_change_statistics",
            )
        except MarketDataUnavailable as exc:
            # Graceful degradation: return stale cache on timeout
            if self._ticker_24h_cache is not None:
                cached_at, stale_rows = self._ticker_24h_cache
                stale_age = now - cached_at
                self._record_endpoint_snapshot(
                    "ticker24hr_price_change_statistics",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=stale_age,
                )
                LOG.info(
                    "fetch_ticker_24h failed, using stale cache | age=%.0fs | error=%s",
                    stale_age,
                    exc.detail,
                )
                return stale_rows
            raise

        new_rows: List[Dict[str, float | str]] = []
        for item in payload if isinstance(payload, list) else []:
            # Handle both dict and object items
            if isinstance(item, dict):
                symbol = str(item.get("symbol", "")).strip().upper()
                last_price = _safe_float(item.get("lastPrice") or item.get("last_price"))
                price_change_percent = _safe_float(
                    item.get("priceChangePercent") or item.get("price_change_percent")
                )
                quote_volume = _safe_float(item.get("quoteVolume") or item.get("quote_volume"))
                trade_count = _safe_float(item.get("count") or item.get("trade_count"))
                if not symbol or last_price <= 0.0 or quote_volume <= 0.0:
                    continue
                new_rows.append(
                    {
                        "symbol": symbol,
                        "last_price": last_price,
                        "price_change_percent": price_change_percent,
                        "quote_volume": quote_volume,
                        "trade_count": trade_count,
                    }
                )
            else:
                symbol = str(getattr(item, "symbol", "")).strip().upper()
                last_price = _safe_float(
                    getattr(item, "last_price", None) or getattr(item, "lastPrice", None)
                )
                price_change_percent = _safe_float(
                    getattr(item, "price_change_percent", 0)
                    or getattr(item, "priceChangePercent", 0)
                )
                quote_volume = _safe_float(
                    getattr(item, "quote_volume", None) or getattr(item, "quoteVolume", None)
                )
                trade_count = _safe_float(
                    getattr(item, "count", None) or getattr(item, "trade_count", None)
                )
                if not symbol or last_price <= 0.0 or quote_volume <= 0.0:
                    continue
                new_rows.append(
                    {
                        "symbol": symbol,
                        "last_price": last_price,
                        "price_change_percent": price_change_percent,
                        "quote_volume": quote_volume,
                        "trade_count": trade_count,
                    }
                )
        self._ticker_24h_cache = (now, new_rows)
        return new_rows

    def _is_cache_valid(self, cache_entry: Tuple[float, Any] | None, ttl_seconds: int) -> bool:
        if cache_entry is None:
            return False
        cached_at, _ = cache_entry
        return (time.monotonic() - cached_at) < ttl_seconds

    async def _fetch_derived_klines_uncached(
        self,
        kind: str,
        symbol: str,
        interval: str,
        *,
        limit: int,
    ) -> Any:
        validate_symbol(symbol)
        validate_interval(interval)
        validate_limit(limit)
        if kind == "continuous":
            contract_type = "PERPETUAL"
            assert (
                contract_type == "PERPETUAL"
            ), f"Only PERPETUAL contracts supported, got {contract_type}"
            try:
                rows = await self._call_public_http_json(
                    "continuous_kline_candlestick_data",
                    params={
                        "pair": symbol,
                        "contractType": contract_type,
                        "interval": interval,
                        "limit": limit,
                    },
                    symbol=symbol,
                )
            except RuntimeError as exc:
                message = str(exc)
                if '"code":-4104' in message or "Invalid contract type" in message:
                    LOG.debug(
                        "continuous klines unsupported for symbol | symbol=%s interval=%s",
                        symbol,
                        interval,
                    )
                    return pl.DataFrame()
                raise
        elif kind == "mark":
            rows = await self._call_public_http_json(
                "mark_price_kline_data",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                symbol=symbol,
            )
        elif kind == "index":
            rows = await self._call_public_http_json(
                "index_price_kline_data",
                params={"pair": symbol, "interval": interval, "limit": limit},
                symbol=symbol,
            )
        else:
            raise ValueError(f"unsupported derived kline kind: {kind!r}")
        return _drop_incomplete_ohlcv_tail(_klines_to_frame(rows), interval)

    async def _fetch_derived_klines_cached(
        self,
        kind: str,
        symbol: str,
        interval: str,
        *,
        limit: int,
    ) -> Any:
        validate_symbol(symbol)
        validate_interval(interval)
        validate_limit(limit)
        cache_ttl_key = {
            "continuous": "continuous_klines",
            "mark": "mark_price_klines",
            "index": "index_price_klines",
        }.get(kind)
        if cache_ttl_key is None:
            raise ValueError(f"unsupported derived kline kind: {kind!r}")
        key = (kind, symbol, interval, int(limit))
        ttl = int(_CACHE_TTL.get(cache_ttl_key, 900))
        now = time.monotonic()
        cached = self._derived_klines_cache.get(key)
        if cached is not None and (now - cached[0]) < ttl:
            self._record_endpoint_snapshot(
                f"{kind}_klines",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1]
        lock = self._derived_klines_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._derived_klines_locks[key] = lock
        try:
            async with lock:
                now = time.monotonic()
                cached = self._derived_klines_cache.get(key)
                if cached is not None and (now - cached[0]) < ttl:
                    self._record_endpoint_snapshot(
                        f"{kind}_klines",
                        source="rest",
                        cache_hit=True,
                        fallback_used=False,
                        response_age_s=now - cached[0],
                    )
                    return cached[1]
                frame = await self._fetch_derived_klines_uncached(
                    kind,
                    symbol,
                    interval,
                    limit=limit,
                )
                self._derived_klines_cache[key] = (time.monotonic(), frame)
                return frame
        finally:
            active_lock = self._derived_klines_locks.get(key)
            if active_lock is lock and not lock.locked():
                self._derived_klines_locks.pop(key, None)

    async def fetch_continuous_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> Any:
        """Fetch public continuous USD-M klines for backtest-stable history."""
        return await self._fetch_derived_klines_cached("continuous", symbol, interval, limit=limit)

    async def fetch_mark_price_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> Any:
        """Fetch public mark-price klines for premium/basis analytics."""
        return await self._fetch_derived_klines_cached("mark", symbol, interval, limit=limit)

    async def fetch_index_price_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> Any:
        """Fetch public index-price klines for spot/futures divergence analytics."""
        return await self._fetch_derived_klines_cached("index", symbol, interval, limit=limit)

    async def fetch_priority_history_bundle(
        self,
        symbol: str,
        *,
        intervals: Tuple[str, ...] = ("15m", "1h", "4h"),
        limit: int = 300,
    ) -> Dict[str, Any]:
        validate_symbol(symbol)
        frames: Dict[str, Any] = {}
        for interval in intervals:
            validate_interval(interval)
            settled_limit = max(1, min(int(limit), 1500))
            fetches = await asyncio.gather(
                self.fetch_klines_cached(symbol, interval, limit=settled_limit),
                self.fetch_continuous_klines(symbol, interval, limit=settled_limit),
                self.fetch_mark_price_klines(symbol, interval, limit=settled_limit),
                self.fetch_index_price_klines(symbol, interval, limit=settled_limit),
                return_exceptions=True,
            )
            for suffix, result in zip(
                ("trade", "continuous", "mark", "index"),
                fetches,
                strict=True,
            ):
                key = f"{interval}:{suffix}"
                if isinstance(result, Exception):
                    LOG.info(
                        "priority history fetch skipped | symbol=%s interval=%s kind=%s error=%s",
                        symbol,
                        interval,
                        suffix,
                        result,
                    )
                    continue
                frames[key] = result
        return frames

    def get_cached_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int,
        max_age_s: float | None = None,
    ) -> Any:
        key = (symbol, interval, int(limit))
        cached = self._klines_cache.get(key)
        if cached is None:
            return None
        cached_at, frame = cached
        ttl = float(
            max_age_s if max_age_s is not None else _CACHE_TTL.get(f"klines_{interval}", 60)
        )
        if time.monotonic() - cached_at > ttl:
            return None
        return frame

    async def fetch_order_book_depth_snapshot(
        self, symbol: str, *, limit: int = _DEFAULT_ORDER_BOOK_DEPTH_LIMIT
    ) -> Dict[str, float | None]:
        validate_symbol(symbol)
        limit = validate_order_book_depth_limit(limit)
        key = (symbol, limit)
        now = time.monotonic()
        cached = self._order_book_depth_cache.get(key)
        ttl = int(_CACHE_TTL["order_book_depth"])
        if cached is not None and (now - cached[0]) < ttl:
            self._record_endpoint_snapshot(
                "order_book_depth",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return dict(cached[1])

        payload = await self._call_public_http_json(
            "order_book_depth",
            params={"symbol": symbol, "limit": limit},
            symbol=symbol,
        )
        if not isinstance(payload, Mapping):
            raise MarketDataUnavailable(
                operation="order_book_depth",
                detail=f"unexpected payload type: {type(payload).__name__}",
                symbol=symbol,
            )

        bids = _parse_depth_levels(payload.get("bids"), reverse=True)
        asks = _parse_depth_levels(payload.get("asks"), reverse=False)
        if not bids or not asks:
            raise MarketDataUnavailable(
                operation="order_book_depth",
                detail="empty order book levels",
                symbol=symbol,
            )

        last_update_raw = payload.get("lastUpdateId") or payload.get("last_update_id")
        try:
            last_update_id = float(last_update_raw) if last_update_raw is not None else None
        except (TypeError, ValueError):
            last_update_id = None
        snapshot: Dict[str, float | None] = {
            "bid_price": bids[0][0],
            "ask_price": asks[0][0],
            "bid_qty": sum(qty for _price, qty in bids),
            "ask_qty": sum(qty for _price, qty in asks),
            "last_update_id": last_update_id,
        }
        self._order_book_depth_cache[key] = (time.monotonic(), snapshot)
        return dict(snapshot)

    async def _fetch_order_book_context_rest_detail(
        self, symbol: str
    ) -> Dict[str, float | None]:
        try:
            return await self.fetch_order_book_depth_snapshot(
                symbol,
                limit=_DEFAULT_ORDER_BOOK_DEPTH_LIMIT,
            )
        except MarketDataUnavailable as exc:
            LOG.info(
                "order book depth unavailable, falling back to book ticker | symbol=%s detail=%s",
                symbol,
                exc.detail,
            )
            return await self._fetch_book_ticker_rest_detail(symbol)

    async def _fetch_book_ticker_rest_detail(self, symbol: str) -> Dict[str, float | None]:
        validate_symbol(symbol)
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                payload = await self._call_public_http_json(
                    "symbol_order_book_ticker",
                    params={"symbol": symbol},
                    symbol=symbol,
                )
                if isinstance(payload, Mapping):
                    bid_raw = payload.get("bidPrice") or payload.get("bid_price")
                    ask_raw = payload.get("askPrice") or payload.get("ask_price")
                    bid_qty_raw = payload.get("bidQty") or payload.get("bid_qty")
                    ask_qty_raw = payload.get("askQty") or payload.get("ask_qty")
                else:
                    bid_raw = getattr(payload, "bid_price", None)
                    ask_raw = getattr(payload, "ask_price", None)
                    bid_qty_raw = getattr(payload, "bid_qty", None)
                    ask_qty_raw = getattr(payload, "ask_qty", None)
                bid = float(bid_raw) if bid_raw is not None else None
                ask = float(ask_raw) if ask_raw is not None else None
                bid_qty = float(bid_qty_raw) if bid_qty_raw is not None else None
                ask_qty = float(ask_qty_raw) if ask_qty_raw is not None else None
                return {
                    "bid_price": bid,
                    "ask_price": ask,
                    "bid_qty": bid_qty,
                    "ask_qty": ask_qty,
                }
            except MarketDataUnavailable as exc:
                detail = (exc.detail or "").lower()
                if attempt < max_attempts and "timeout" in detail:
                    backoff = min(2.0, 0.5 * (2 ** (attempt - 1))) * random.uniform(0.9, 1.1)
                    LOG.info(
                        "book ticker retry | symbol=%s attempt=%d/%d backoff=%.2fs detail=%s",
                        symbol,
                        attempt,
                        max_attempts,
                        backoff,
                        detail,
                    )
                    await asyncio.sleep(backoff)
                    continue
                LOG.info(
                    "book ticker unavailable, returning empty prices | symbol=%s detail=%s",
                    symbol,
                    detail,
                )
                return {
                    "bid_price": None,
                    "ask_price": None,
                    "bid_qty": None,
                    "ask_qty": None,
                }
        return {
            "bid_price": None,
            "ask_price": None,
            "bid_qty": None,
            "ask_qty": None,
        }

    async def _fetch_book_ticker_rest(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        detail = await self._fetch_book_ticker_rest_detail(symbol)
        return detail.get("bid_price"), detail.get("ask_price")

    async def _fetch_agg_trade_snapshot_rest(
        self, symbol: str, *, limit: int = 100
    ) -> AggTradeSnapshot:
        validate_symbol(symbol)
        validate_limit(limit, max_val=1000)
        payload = await self._call_public_http_json(
            "compressed_aggregate_trades_list",
            params={"symbol": symbol, "limit": limit},
            symbol=symbol,
        )
        buy_qty = 0.0
        sell_qty = 0.0
        trade_count = 0
        payload_rows = cast(List[Any], payload)
        for item in payload_rows:
            row = _coerce_rest_row(item)
            qty = float(row.get("q") or 0.0)
            is_buyer_maker = bool(row.get("m"))
            trade_count += 1
            if is_buyer_maker:
                sell_qty += qty
            else:
                buy_qty += qty
        total_qty = buy_qty + sell_qty
        delta_ratio = None
        if total_qty > 0:
            delta_ratio = (buy_qty - sell_qty) / total_qty
        return AggTradeSnapshot(
            symbol=symbol,
            trade_count=trade_count,
            buy_qty=buy_qty,
            sell_qty=sell_qty,
            delta_ratio=delta_ratio,
        )

    async def fetch_book_ticker(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        if self._ws is not None:
            cached = await self._ws.get_book_ticker(symbol)
            if cached is not None:
                return cached
        return await self._fetch_book_ticker_rest(symbol)

    async def fetch_agg_trade_snapshot(self, symbol: str, *, limit: int = 100) -> AggTradeSnapshot:
        if self._ws is not None:
            snapshot = self._ws.get_agg_trade_snapshot(symbol)
            if snapshot is not None:
                return snapshot
        return await self._fetch_agg_trade_snapshot_rest(symbol, limit=limit)

    async def fetch_agg_trades(
        self,
        symbol: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        page_limit: int,
        page_size: int,
    ) -> Tuple[List[AggTrade], bool]:
        validate_symbol(symbol)
        rows: List[AggTrade] = []
        pages = 0
        complete = True
        window_start_ms = max(int(start_time_ms), 0)
        final_end_ms = min(max(int(end_time_ms), 0), int(time.time() * 1000))
        max_window_ms = 3_599_000
        while pages < page_limit and window_start_ms <= final_end_ms:
            window_end_ms = min(window_start_ms + max_window_ms, final_end_ms)
            next_from_id: int | None = None
            while pages < page_limit:
                kwargs: Dict[str, Any] = {"symbol": symbol, "limit": page_size}
                if next_from_id is None:
                    kwargs["startTime"] = window_start_ms
                    kwargs["endTime"] = window_end_ms
                else:
                    kwargs["fromId"] = next_from_id
                payload = await self._call_public_http_json(
                    "compressed_aggregate_trades_list",
                    params=kwargs,
                    symbol=symbol,
                )
                batch: List[AggTrade] = []
                payload_rows = cast(List[Any], payload)
                for item in payload_rows:
                    row = _coerce_rest_row(item)
                    trade_time_ms = int(row.get("T") or 0)
                    if trade_time_ms < start_time_ms:
                        continue
                    if trade_time_ms > end_time_ms:
                        continue
                    trade_id = int(row.get("a") or 0)
                    batch.append(
                        AggTrade(
                            symbol=symbol,
                            trade_id=trade_id,
                            price=float(row.get("p") or 0.0),
                            quantity=float(row.get("q") or 0.0),
                            trade_time_ms=trade_time_ms,
                            is_buyer_maker=bool(row.get("m")),
                        )
                    )
                if not payload_rows:
                    break
                rows.extend(batch)
                pages += 1
                last_row = _coerce_rest_row(payload_rows[-1])
                next_from_id = int(last_row.get("a") or 0) + 1
                last_time_ms = int(last_row.get("T") or 0)
                if len(payload_rows) < page_size or last_time_ms >= window_end_ms:
                    break
                await asyncio.sleep(0.05)
            if pages >= page_limit and window_end_ms < final_end_ms:
                complete = False
                break
            window_start_ms = window_end_ms + 1
        deduped: Dict[int, AggTrade] = {}
        for item in rows:
            deduped[item.trade_id] = item
        sorted_rows = sorted(deduped.values(), key=lambda item: (item.trade_time_ms, item.trade_id))
        if sorted_rows and sorted_rows[-1].trade_time_ms < end_time_ms and pages >= page_limit:
            complete = False
        return sorted_rows, complete

    async def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        validate_symbol(symbol)
        now = time.monotonic()
        cached = self._funding_rate_cache.get(symbol)
        if cached is not None and now - cached[0] < 300:
            self._record_endpoint_snapshot(
                "premium_index",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1]

        try:
            payload = await self._call_public_http_json(
                "premium_index",
                params={"symbol": symbol},
                symbol=symbol,
            )
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot(
                    "premium_index",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=now - cached[0],
                )
                return cached[1]
            raise

        value_raw = payload.get("lastFundingRate") or payload.get("last_funding_rate")
        value = float(value_raw) if value_raw is not None else None
        if value is not None:
            self._funding_rate_cache[symbol] = (now, value)
        return value

    async def fetch_premium_index_all(self) -> Dict[str, Dict[str, float]]:
        now = time.monotonic()
        cached = self._premium_index_all_cache
        if cached is not None and now - cached[0] < 300:
            self._record_endpoint_snapshot(
                "premium_index",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1]

        try:
            payload = await self._call_public_http_json("premium_index")
        except MarketDataUnavailable as exc:
            if cached is not None:
                self._record_endpoint_snapshot(
                    "premium_index",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=now - cached[0],
                )
                LOG.info(
                    "fetch_premium_index_all failed, using stale cache | age=%.0fs error=%s",
                    now - cached[0],
                    exc.detail,
                )
                return cached[1]
            raise
        rows: Dict[str, Dict[str, float]] = {}
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, Mapping):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            funding_rate = _safe_float(item.get("lastFundingRate"))
            mark_price = _safe_float(item.get("markPrice"))
            index_price = _safe_float(item.get("indexPrice"))
            basis_pct = (
                ((mark_price - index_price) / index_price) * 100.0
                if mark_price > 0.0 and index_price > 0.0
                else 0.0
            )
            rows[symbol] = {
                "funding_rate": funding_rate,
                "basis_pct": basis_pct,
                "mark_price": mark_price,
                "index_price": index_price,
            }
            self._funding_rate_cache[symbol] = (now, funding_rate)
            self._basis_cache[(symbol, "1h")] = (now, basis_pct)
        self._premium_index_all_cache = (now, rows)
        return rows

    async def fetch_open_interest(self, symbol: str) -> Optional[float]:
        validate_symbol(symbol)
        now = time.monotonic()
        cached = self._open_interest_cache.get(symbol)
        if self._is_cache_valid(cached, _CACHE_TTL["open_interest"]):
            assert cached is not None
            self._record_endpoint_snapshot(
                "open_interest",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1] if cached else None

        try:
            payload = await self._call_public_http_json(
                "open_interest",
                params={"symbol": symbol},
                symbol=symbol,
            )
            row = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
            value_raw = row.get("open_interest") or row.get("openInterest")
            value = float(value_raw) if value_raw is not None else None
            if value is not None:
                self._open_interest_cache[symbol] = (now, value)
            return value
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot(
                    "open_interest",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=now - cached[0],
                )
                LOG.debug("OI graceful degradation | symbol=%s using stale cache", symbol)
                return cached[1]
            return None

    async def fetch_open_interest_change(
        self, symbol: str, *, period: str = "1h"
    ) -> Optional[float]:
        validate_symbol(symbol)
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._open_interest_change_cache.get(cache_key)
        if self._is_cache_valid(cached, _CACHE_TTL["open_interest_change"]):
            assert cached is not None
            self._record_endpoint_snapshot(
                "open_interest_statistics",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1] if cached else None

        try:
            payload = await self._call_public_http_json(
                "open_interest_statistics",
                params={"symbol": symbol, "period": period, "limit": 2},
                symbol=symbol,
            )
            if not payload:
                return None
            rows = [
                item.model_dump() if hasattr(item, "model_dump") else dict(item)
                for item in payload
            ]
            rows.sort(key=lambda row: int(row.get("timestamp") or 0))
            if len(rows) < 2:
                return None
            prev_raw = rows[-2].get("sumOpenInterest") or rows[-2].get("sum_open_interest")
            curr_raw = rows[-1].get("sumOpenInterest") or rows[-1].get("sum_open_interest")
            prev = float(prev_raw) if prev_raw is not None else 0.0
            curr = float(curr_raw) if curr_raw is not None else 0.0
            if prev <= 0.0:
                return None
            change = (curr / prev) - 1.0
            self._open_interest_change_cache[cache_key] = (now, change)
            return change
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot(
                    "open_interest_statistics",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=now - cached[0],
                )
                LOG.debug(
                    "OI change graceful degradation | symbol=%s period=%s using stale cache",
                    symbol,
                    period,
                )
                return cached[1]
            return None

    async def fetch_long_short_ratio(
        self, symbol: str, *, period: str = "1h"
    ) -> Optional[float]:
        validate_symbol(symbol)
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._long_short_ratio_cache.get(cache_key)
        if self._is_cache_valid(cached, _CACHE_TTL["long_short_ratio"]):
            assert cached is not None
            self._record_endpoint_snapshot(
                "top_trader_long_short_ratio_accounts",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1] if cached else None

        try:
            payload = await self._call_public_http_json(
                "top_trader_long_short_ratio_accounts",
                params={"symbol": symbol, "period": period, "limit": 1},
                symbol=symbol,
            )
            if not payload:
                return None
            item = payload[0]
            row = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            value_raw = row.get("longShortRatio") or row.get("long_short_ratio")
            value = float(value_raw) if value_raw is not None else None
            if value is not None:
                self._long_short_ratio_cache[cache_key] = (now, value)
            return value
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot(
                    "top_trader_long_short_ratio_accounts",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=now - cached[0],
                )
                LOG.debug(
                    "L/S ratio graceful degradation | symbol=%s period=%s using stale cache",
                    symbol,
                    period,
                )
                return cached[1]
            return None

    async def fetch_top_position_ls_ratio(
        self, symbol: str, *, period: str = "1h"
    ) -> Optional[float]:
        validate_symbol(symbol)
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._top_position_ls_ratio_cache.get(cache_key)
        if self._is_cache_valid(cached, _CACHE_TTL["long_short_ratio"]):
            assert cached is not None
            self._record_endpoint_snapshot(
                "top_trader_long_short_ratio_positions",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1]

        try:
            payload = await self._call_public_http_json(
                "top_trader_long_short_ratio_positions",
                params={"symbol": symbol, "period": period, "limit": 1},
                symbol=symbol,
            )
            if not payload:
                return None
            item = payload[0]
            row = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            value_raw = row.get("longShortRatio") or row.get("long_short_ratio")
            value = float(value_raw) if value_raw is not None else None
            if value is not None:
                self._top_position_ls_ratio_cache[cache_key] = (now, value)
            return value
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot(
                    "top_trader_long_short_ratio_positions",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=now - cached[0],
                )
                return cached[1]
            return None

    async def fetch_taker_ratio(self, symbol: str, *, period: str = "1h") -> Optional[float]:
        validate_symbol(symbol)
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._taker_ratio_cache.get(cache_key)
        if cached is not None and now - cached[0] < 1200:
            self._record_endpoint_snapshot(
                "taker_long_short_ratio",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1]

        try:
            payload = await self._call_public_http_json(
                "taker_long_short_ratio",
                params={"symbol": symbol, "period": period, "limit": 1},
                symbol=symbol,
            )
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot(
                    "taker_long_short_ratio",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=now - cached[0],
                )
                return cached[1]
            return None

        if not payload:
            return None
        item = payload[0] if isinstance(payload, list) else payload
        raw = item.get("buySellRatio") or item.get("buy_sell_ratio")
        value = float(raw) if raw is not None else None
        if value is not None:
            self._taker_ratio_cache[cache_key] = (now, value)
        return value

    async def fetch_global_ls_ratio(self, symbol: str, *, period: str = "1h") -> Optional[float]:
        validate_symbol(symbol)
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._global_ls_ratio_cache.get(cache_key)
        if cached is not None and now - cached[0] < 1200:
            self._record_endpoint_snapshot(
                "global_long_short_account_ratio",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1]

        try:
            payload = await self._call_public_http_json(
                "global_long_short_account_ratio",
                params={"symbol": symbol, "period": period, "limit": 1},
                symbol=symbol,
            )
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot(
                    "global_long_short_account_ratio",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=now - cached[0],
                )
                return cached[1]
            return None

        if not payload:
            return None
        item = payload[0] if isinstance(payload, list) else payload
        raw = item.get("longShortRatio") or item.get("long_short_ratio")
        value = float(raw) if raw is not None else None
        if value is not None:
            self._global_ls_ratio_cache[cache_key] = (now, value)
        return value

    async def fetch_funding_rate_history(
        self, symbol: str, *, limit: int = 10
    ) -> List[Dict[str, Any]]:
        validate_symbol(symbol)
        validate_limit(limit, max_val=100)
        now = time.monotonic()
        cached = self._funding_history_cache.get(symbol)
        if cached is not None and now - cached[0] < 900:
            self._record_endpoint_snapshot(
                "funding_rate_history",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1]

        try:
            payload = await self._call_public_http_json(
                "funding_rate_history",
                params={"symbol": symbol, "limit": limit},
                symbol=symbol,
            )
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot(
                    "funding_rate_history",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=now - cached[0],
                )
                return cached[1]
            return []

        if not isinstance(payload, list):
            return []

        rows = []
        for item in payload:
            try:
                rows.append(
                    {
                        "fundingTime": int(item.get("fundingTime") or 0),
                        "fundingRate": float(item.get("fundingRate") or 0.0),
                        "markPrice": float(item.get("markPrice") or 0.0),
                    }
                )
            except (TypeError, ValueError):
                continue

        rows.sort(key=lambda r: r["fundingTime"])
        self._funding_history_cache[symbol] = (now, rows)
        return rows

    async def _fetch_symbol_frames_rest(self, symbol: str) -> SymbolFrames:
        frame_4h, frame_1h, frame_15m, frame_5m, book_context = await asyncio.gather(
            self.fetch_klines_cached(symbol, "4h", limit=_DEFAULT_KLINE_FETCH_LIMIT),
            self.fetch_klines_cached(symbol, "1h", limit=_DEFAULT_KLINE_FETCH_LIMIT),
            self.fetch_klines_cached(symbol, "15m", limit=_DEFAULT_KLINE_FETCH_LIMIT),
            self.fetch_klines_cached(symbol, "5m", limit=_DEFAULT_KLINE_FETCH_LIMIT),
            self._fetch_order_book_context_rest_detail(symbol),
        )
        return SymbolFrames(
            symbol=symbol,
            df_1h=frame_1h,
            df_15m=frame_15m,
            bid_price=book_context.get("bid_price"),
            ask_price=book_context.get("ask_price"),
            df_5m=frame_5m,
            df_4h=frame_4h,
            bid_qty=book_context.get("bid_qty"),
            ask_qty=book_context.get("ask_qty"),
        )

    async def preflight_check(self) -> None:
        await self.fetch_exchange_symbols()
        await self.fetch_ticker_24h()

    async def fetch_klines(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        validate_symbol(symbol)
        validate_interval(interval)
        validate_limit(limit)
        rows = await self._call_public_http_json(
            "kline_candlestick_data",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            symbol=symbol,
        )
        frame = _drop_incomplete_ohlcv_tail(_klines_to_frame(rows), interval)
        return frame

    async def fetch_klines_cached(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        """Fetch klines with a TTL cache to prevent REST stampedes."""
        validate_symbol(symbol)
        validate_interval(interval)
        validate_limit(limit)
        key = (symbol, interval, int(limit))
        ttl = int(_CACHE_TTL.get(f"klines_{interval}", 60))
        now = time.monotonic()
        cached = self._klines_cache.get(key)
        if cached is not None and (now - cached[0]) < ttl:
            self._record_endpoint_snapshot(
                "kline_candlestick_data",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1]
        lock = self._klines_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._klines_locks[key] = lock
        try:
            async with lock:
                now = time.monotonic()
                cached = self._klines_cache.get(key)
                if cached is not None and (now - cached[0]) < ttl:
                    self._record_endpoint_snapshot(
                        "kline_candlestick_data",
                        source="rest",
                        cache_hit=True,
                        fallback_used=False,
                        response_age_s=now - cached[0],
                    )
                    frame = cached[1]
                else:
                    frame = await self.fetch_klines(symbol, interval, limit=limit)
                    self._klines_cache[key] = (time.monotonic(), frame)
                    self._record_endpoint_snapshot(
                        "kline_candlestick_data",
                        source="rest",
                        cache_hit=False,
                        fallback_used=False,
                        response_age_s=0.0,
                    )
                    return frame
        finally:
            # Clean up lock if no longer needed (optional)
            pass

    def _record_endpoint_snapshot(
        self,
        endpoint_name: str,
        *,
        source: str,
        cache_hit: bool,
        fallback_used: bool,
        limiter_wait_ms: float = 0.0,
        response_age_s: float | None = None,
    ) -> None:
        self._last_endpoint_name = endpoint_name
        self._last_endpoint_source = source
        self._last_endpoint_cache_hit = bool(cache_hit)
        self._last_endpoint_fallback_used = bool(fallback_used)
        self._last_endpoint_limiter_wait_ms = max(0.0, float(limiter_wait_ms))
        self._last_endpoint_response_age_s = (
            None if response_age_s is None else max(0.0, float(response_age_s))
        )

    def _endpoint_spec(self, operation: str) -> _PublicEndpointSpec:
        try:
            return _PUBLIC_ENDPOINT_REGISTRY[operation]
        except KeyError as exc:
            raise ValueError(f"unsupported public endpoint operation={operation}") from exc

    def _endpoint_url(self, operation: str) -> str:
        spec = self._endpoint_spec(operation)
        return f"{_FAPI_BASE_URL}{spec.path}"


    def get_cached_oi_change(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> Optional[float]:
        cached = self._open_interest_change_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_open_interest(
        self, symbol: str, max_age_s: float = 1800.0
    ) -> Optional[float]:
        cached = self._open_interest_cache.get(symbol)
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> Optional[float]:
        cached = self._long_short_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_funding_rate(self, symbol: str, max_age_s: float = 1800.0) -> Optional[float]:
        cached = self._funding_rate_cache.get(symbol)
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_premium_index(
        self, symbol: str, max_age_s: float = 300.0
    ) -> Optional[Dict[str, float]]:
        cached = self._premium_index_all_cache
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value.get(symbol)

    def get_cached_top_position_ls_ratio(
        self,
        symbol: str,
        period: str = "1h",
        max_age_s: float = 1800.0,
    ) -> Optional[float]:
        cached = self._top_position_ls_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_taker_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> Optional[float]:
        cached = self._taker_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_global_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> Optional[float]:
        cached = self._global_ls_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_funding_trend(self, symbol: str, max_age_s: float = 1800.0) -> Optional[str]:
        cached = self._funding_history_cache.get(symbol)
        if cached is None:
            return None
        cached_at, rows = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        if len(rows) < 3:
            return None
        recent = [r["fundingRate"] for r in rows[-4:]]
        # Count directional steps
        ups = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
        downs = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])
        steps = len(recent) - 1
        if ups >= steps * 0.75:
            return "rising"
        if downs >= steps * 0.75:
            return "falling"
        return "flat"

    def get_cached_funding_recent_extreme(
        self,
        symbol: str,
        *,
        max_age_hours: float = 48.0,
        max_cache_age_s: float = 1800.0,
    ) -> Optional[Tuple[float, float]]:
        cached = self._funding_history_cache.get(symbol)
        if cached is None:
            return None
        cached_at, rows = cached
        if time.monotonic() - cached_at > max_cache_age_s:
            return None
        if not rows:
            return None
        now_ms = int(time.time() * 1000)
        max_age_ms = max(0.0, float(max_age_hours)) * 3600.0 * 1000.0
        candidates: List[Tuple[float, float]] = []
        for row in rows:
            try:
                rate = float(row.get("fundingRate") or 0.0)
                funding_time = int(row.get("fundingTime") or 0)
            except (TypeError, ValueError):
                continue
            if funding_time <= 0:
                continue
            age_ms = max(0, now_ms - funding_time)
            if age_ms <= max_age_ms:
                candidates.append((rate, age_ms / 3600000.0))
        if not candidates:
            return None
        return max(candidates, key=lambda item: abs(item[0]))

    async def fetch_basis(self, symbol: str, *, period: str = "1h", limit: int = 3) -> Optional[float]:
        validate_symbol(symbol)
        """Fetch most recent basis (futures - index price as %) from /futures/data/basis.

        Returns basis as a percentage (positive = contango, negative = backwardation).
        Cached for 900 seconds.
        """
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._basis_cache.get(cache_key)
        if cached is not None and now - cached[0] < 900:
            self._record_endpoint_snapshot(
                "basis",
                source="rest",
                cache_hit=True,
                fallback_used=False,
                response_age_s=now - cached[0],
            )
            return cached[1]

        try:
            payload = await self._call_public_http_json(
                "basis",
                params={
                    "pair": symbol,
                    "contractType": "PERPETUAL",
                    "period": period,
                    "limit": limit,
                },
                symbol=symbol,
            )
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot(
                    "basis",
                    source="rest",
                    cache_hit=True,
                    fallback_used=True,
                    response_age_s=now - cached[0],
                )
                return cached[1]
            return None

        if not isinstance(payload, list) or not payload:
            return None

        # Sort by timestamp, take the most recent
        payload.sort(key=lambda r: int(r.get("timestamp") or 0))
        basis_series: List[float] = []
        for row in payload:
            try:
                futures_price = float(row.get("futuresPrice") or 0.0)
                index_price = float(row.get("indexPrice") or 0.0)
            except (TypeError, ValueError):
                continue
            if index_price <= 0.0:
                continue
            basis_series.append((futures_price - index_price) / index_price * 100.0)
        if not basis_series:
            return None
        basis_pct = basis_series[-1]
        premium_slope = None
        if len(basis_series) >= 2:
            premium_slope = basis_series[-1] - basis_series[-2]
        premium_zscore = None
        if len(basis_series) >= 3:
            mean = sum(basis_series) / len(basis_series)
            variance = sum((value - mean) ** 2 for value in basis_series) / len(basis_series)
            std = math.sqrt(variance)
            if std > 0.0:
                premium_zscore = (basis_series[-1] - mean) / std

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

    def get_cached_basis(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> Optional[float]:
        """Return cached basis pct if fresh, else None (no REST call)."""
        cached = self._basis_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_basis_stats(
        self,
        symbol: str,
        period: str = "1h",
        max_age_s: float = 1800.0,
    ) -> Optional[Dict[str, Optional[float]]]:
        cached = self._basis_stats_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return dict(value)

    def update_basis_from_websocket(
        self,
        symbol: str,
        mark_price: float,
        index_price: Optional[float] = None,
        period: str = "5m",
    ) -> Optional[Dict[str, Optional[float]]]:
        """Update basis cache from WebSocket mark price data (zero I/O).

        If index_price is None, uses mark_price as fallback (spread = 0).
        Returns calculated stats dict or None if inputs invalid.
        """
        if mark_price <= 0.0:
            return None

        now = time.monotonic()
        cache_key = (symbol, period)
        window_seconds = _PERIOD_WINDOW_SECONDS.get(period, 300)

        basis_pct: Optional[float]
        if index_price is not None and index_price > 0.0:
            basis_pct = (mark_price - index_price) / index_price * 100.0
            mark_index_spread_bps = basis_pct * 100.0  # Convert to bps
        else:
            # No index price available - use cached basis or mark price
            cached = self._basis_cache.get(cache_key)
            if cached is not None:
                basis_pct = cached[1]  # Use existing basis
            else:
                basis_pct = None
            mark_index_spread_bps = None  # Can't calculate without index

        if basis_pct is not None:
            self._basis_cache[cache_key] = (now, basis_pct)
            history = self._basis_ws_history.get(cache_key)
            if history is None:
                history = deque(maxlen=max(window_seconds * 2, 600))
                self._basis_ws_history[cache_key] = history
            history.append((now, basis_pct))
            while history and (now - history[0][0]) > window_seconds:
                history.popleft()
            basis_values = [value for _, value in history]
        else:
            basis_values = []

        premium_slope = None
        if len(basis_values) >= 2:
            premium_slope = basis_values[-1] - basis_values[0]

        premium_zscore = None
        if len(basis_values) >= 3:
            mean = sum(basis_values) / len(basis_values)
            variance = sum((value - mean) ** 2 for value in basis_values) / len(basis_values)
            std = math.sqrt(variance)
            if std > 0.0:
                premium_zscore = (basis_values[-1] - mean) / std

        stats = {
            "latest_basis_pct": basis_pct,
            "premium_slope_5m": premium_slope,
            "premium_zscore_5m": premium_zscore,
            "mark_index_spread_bps": mark_index_spread_bps,
        }
        self._basis_stats_cache[cache_key] = (now, stats)

        return stats

    async def fetch_symbol_frames(self, symbol: str) -> SymbolFrames:
        if self._ws is not None and hasattr(self._ws, 'is_warm') and self._ws.is_warm(symbol):
            frames = await self._ws.get_symbol_frames(symbol)
            if frames is not None:
                return frames
        return await self._fetch_symbol_frames_rest(symbol)



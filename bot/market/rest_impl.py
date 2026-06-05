"""Binance USD-M public REST client (single module)."""

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
from typing import Any, cast
from urllib.parse import urlparse

import aiohttp
import polars as pl

from bot.domain.config import NetworkConfig
from bot.domain.schemas import (
    AggTrade,
    AggTradeSnapshot,
    SymbolFrames,
    SymbolMeta,
)
from bot.market.data import (
    _ALLOWED_PUBLIC_REST_PATHS,
    _CACHE_TTL,
    _DEFAULT_KLINE_FETCH_LIMIT,
    _DEFAULT_ORDER_BOOK_DEPTH_LIMIT,
    _ENDPOINT_WEIGHTS,
    _FALLBACK_TIMEOUT_DEBUG_OPERATIONS,
    _REST_TIMEOUT_WARNING_OPERATIONS,
    _FAPI_BASE_URL,
    _FORBIDDEN_PARAMS_LOWER,
    _FORBIDDEN_PUBLIC_PATH_MARKERS,
    _FUTURES_DATA_IP_LIMIT_DEFAULT,
    _FUTURES_DATA_IP_LIMIT_OFFICIAL_MAX,
    _FUTURES_DATA_IP_LIMIT_WINDOW_S,
    _FUTURES_DATA_REQUEST_LIMITED_OPS,
    _HTTP_CONNECTOR_LIMIT,
    _KLINE_COLUMNS,
    _KLINE_FRAME_SCHEMA,
    _PERIOD_WINDOW_SECONDS,
    _PUBLIC_ENDPOINT_REGISTRY,
    _REST_WEIGHT_HARD_LIMIT,
    _REST_WEIGHT_PACE_LIMIT,
    _REST_WEIGHT_SOFT_LIMIT,
    _VALID_INTERVALS,
    _VALID_ORDER_BOOK_DEPTH_LIMITS,
    FORBIDDEN_PARAMS,
    UTC,
    MarketDataUnavailable,
    _PublicEndpointSpec,
    rest_global_semaphore,
)
from bot.market.network_proxy import (
    aiohttp_request_proxy,
    apply_proxy_env,
    close_aiohttp_session,
    create_aiohttp_session,
    mask_proxy_url,
    resolve_proxy_url,
)
from bot.market.proxy_pool import ProxyPool, is_proxy_transport_error
from bot.market.rate_limit import _SlidingWindowRateLimiter, _WeightBudgetManager

# --- validators ---


def validate_symbol(symbol: str) -> None:
    """Validate Binance symbol format (e.g., BTCUSDT)."""
    if not symbol or not isinstance(symbol, str):
        msg = f"invalid symbol type or empty: {symbol!r}"
        raise ValueError(msg)
    if not symbol.isalnum():
        msg = f"symbol must be alphanumeric: {symbol!r}"
        raise ValueError(msg)
    if symbol != symbol.upper():
        msg = f"symbol must be uppercase: {symbol!r}"
        raise ValueError(msg)


def validate_interval(interval: str) -> None:
    """Validate Binance kline interval."""
    if interval not in _VALID_INTERVALS:
        msg = f"unsupported binance interval: {interval!r}"
        raise ValueError(msg)


def validate_limit(limit: int, min_val: int = 1, max_val: int = 1500) -> None:
    """Validate request limit range."""
    if not isinstance(limit, int):
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            msg = f"limit must be an integer: {limit!r}"
            raise ValueError(msg) from None
    if limit < min_val or limit > max_val:
        msg = f"limit out of range [{min_val}, {max_val}]: {limit}"
        raise ValueError(msg)


def validate_order_book_depth_limit(limit: int) -> int:
    """Validate Binance USD-M order-book depth snapshot limit."""
    try:
        normalized = int(limit)
    except (ValueError, TypeError) as exc:
        msg = f"order book depth limit must be an integer: {limit!r}"
        raise ValueError(msg) from exc
    if normalized not in _VALID_ORDER_BOOK_DEPTH_LIMITS:
        allowed = ", ".join(str(value) for value in sorted(_VALID_ORDER_BOOK_DEPTH_LIMITS))
        msg = f"order book depth limit must be one of [{allowed}]: {normalized}"
        raise ValueError(msg)
    return normalized


def validate_runtime_public_rest_url(url: str) -> None:
    """Validate runtime public REST URL."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        msg = f"invalid public REST URL: {url!r}"
        raise ValueError(msg)
    if parsed.scheme not in ("http", "https"):
        msg = f"unsupported protocol in public REST URL: {url!r}"
        raise ValueError(msg)
    if not any(parsed.path.startswith(prefix) for prefix in _ALLOWED_PUBLIC_REST_PATHS):
        msg = f"public REST URL must start with one of {_ALLOWED_PUBLIC_REST_PATHS}: {url!r}"
        raise ValueError(msg)
    if any(marker in url.lower() for marker in _FORBIDDEN_PUBLIC_PATH_MARKERS):
        msg = f"public REST URL contains forbidden marker: {url!r}"
        raise ValueError(msg)


def _validate_rest_params(params: Mapping[str, Any] | None) -> None:
    if params is None:
        return
    for key in params:
        key_text = str(key)
        if key_text in FORBIDDEN_PARAMS:
            msg = f"forbidden parameter: {key}"
            raise ValueError(msg)
        if key_text.lower() in _FORBIDDEN_PARAMS_LOWER:
            msg = f"forbidden parameter: {key}"
            raise ValueError(msg)


# --- frames ---
def _timeframe_to_seconds(timeframe: str) -> int | None:
    mapping = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "6h": 21600,
        "8h": 28800,
        "12h": 43200,
        "1d": 86400,
    }
    return mapping.get(timeframe)


def _ohlcv_frame_has_incomplete_tail(df: pl.DataFrame, timeframe: str) -> bool:
    if df.is_empty():
        return False
    if "close_time" in df.columns:
        last_close = df["close_time"].tail(1).item()
        if isinstance(last_close, datetime):
            return datetime.now(UTC) <= last_close
    timeframe_seconds = _timeframe_to_seconds(timeframe)
    if timeframe_seconds is None:
        return False
    last_open = df["time"].tail(1).item()
    if not isinstance(last_open, datetime):
        return False
    return datetime.now(UTC) < last_open + timedelta(seconds=timeframe_seconds)


def _drop_incomplete_ohlcv_tail(df: pl.DataFrame, timeframe: str) -> pl.DataFrame:
    if df.is_empty():
        return df
    if "close_time" in df.columns:
        now = datetime.now(UTC)
        closed = df.filter(pl.col("close_time") < pl.lit(now))
        if closed.height != df.height:
            return closed
    if _ohlcv_frame_has_incomplete_tail(df, timeframe):
        return df.head(df.height - 1)
    return df


def _klines_to_frame(rows: Any) -> pl.DataFrame:
    """Convert raw Binance kline rows into a Polars DataFrame using vectorized operations.

    Expected REST input is a list of lists with at least 11 items. The function
    also accepts dict rows from WebSocket/backfill paths so callers can share
    one conversion boundary without silently returning an empty frame.
    """
    if not rows:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)

    columns = list(_KLINE_COLUMNS)
    valid_rows: list[list[Any]] = []
    dict_rows: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, list) and len(row) >= 11:
            valid_rows.append(row[:11])
            continue
        if isinstance(row, Mapping):
            dict_rows.append({column: row.get(column) for column in columns})

    if not valid_rows and not dict_rows:
        return pl.DataFrame(schema=_KLINE_FRAME_SCHEMA)

    frames: list[pl.DataFrame] = []
    if valid_rows:
        frames.append(pl.DataFrame(valid_rows, schema=columns, orient="row"))
    if dict_rows:
        frames.append(pl.DataFrame(dict_rows))
    frame = frames[0] if len(frames) == 1 else pl.concat(frames, how="diagonal")

    time_exprs: list[pl.Expr] = []
    for column in ("time", "close_time"):
        dtype = frame.schema.get(column)
        if dtype is not None and getattr(dtype, "is_temporal", lambda: False)():
            time_exprs.append(pl.col(column))
        elif dtype == pl.String:
            time_exprs.append(pl.col(column).str.to_datetime(strict=False, time_zone="UTC"))
        else:
            time_exprs.append(
                pl.from_epoch(pl.col(column).cast(pl.Int64), time_unit="ms").dt.replace_time_zone(
                    "UTC"
                )
            )

    return frame.with_columns(
        [
            time_exprs[0].alias("time"),
            time_exprs[1].alias("close_time"),
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("volume").cast(pl.Float64),
            pl.col("quote_volume").cast(pl.Float64),
            pl.col("num_trades").cast(pl.Int64),
            pl.col("taker_buy_base_volume").cast(pl.Float64),
            pl.col("taker_buy_quote_volume").cast(pl.Float64),
            pl.from_epoch(pl.col("time"), time_unit="ms")
            .dt.replace_time_zone("UTC")
            .alias("open_time"),
        ]
    )


def _unwrap_model(value: Any) -> Any:
    if hasattr(value, "actual_instance") and value.actual_instance is not None:
        return value.actual_instance
    return value


def _coerce_rest_row(item: Any) -> Mapping[str, Any]:
    row = _unwrap_model(item)
    if isinstance(row, Mapping):
        return row
    if hasattr(row, "model_dump"):
        dumped = row.model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    msg = f"Unsupported REST row payload type: {type(item)!r}"
    raise TypeError(msg)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _parse_depth_levels(raw_levels: Any, *, reverse: bool) -> tuple[tuple[float, float], ...]:
    parsed: list[tuple[float, float]] = []
    if not isinstance(raw_levels, list):
        return ()
    for raw in raw_levels:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        try:
            price = float(raw[0])
            qty = float(raw[1])
        except (TypeError, ValueError):
            continue
        if price <= 0.0 or qty <= 0.0 or not math.isfinite(price) or not math.isfinite(qty):
            continue
        parsed.append((price, qty))
    parsed.sort(key=lambda item: item[0], reverse=reverse)
    return tuple(parsed)


# --- abc ---

LOG = logging.getLogger("bot.market.rest")


from bot.market._rest_circuit import RestCircuitMixin
from bot.market._rest_frames import (
    _coerce_rest_row,
    _drop_incomplete_ohlcv_tail,
    _klines_to_frame,
    _ohlcv_frame_has_incomplete_tail,
    _parse_depth_levels,
    _safe_float,
    _timeframe_to_seconds,
    _unwrap_model,
)

class BinanceClient(ABC):
    """Abstract interface for Binance API client."""

    @abstractmethod
    async def fetch_exchange_symbols(self) -> list[SymbolMeta]:
        """Fetch exchange symbols information."""

    @abstractmethod
    async def fetch_ticker_24h(self) -> list[dict[str, float | str]]:
        """Fetch 24hr ticker statistics."""

    @abstractmethod
    async def fetch_klines(self, symbol: str, interval: str, *, limit: int) -> Any:
        """Fetch kline/candlestick data."""

    @abstractmethod
    async def fetch_klines_cached(self, symbol: str, interval: str, *, limit: int) -> Any:
        """Fetch klines with caching."""

    @abstractmethod
    async def fetch_continuous_klines(self, symbol: str, interval: str, *, limit: int=500) -> Any:
        """Fetch continuous klines."""

    @abstractmethod
    async def fetch_mark_price_klines(self, symbol: str, interval: str, *, limit: int=500) -> Any:
        """Fetch mark price klines."""

    @abstractmethod
    async def fetch_index_price_klines(self, symbol: str, interval: str, *, limit: int=500) -> Any:
        """Fetch index price klines."""

    @abstractmethod
    async def fetch_priority_history_bundle(self, symbol: str, *, intervals: tuple[str, ...]=('15m', '1h', '4h'), limit: int=300) -> dict[str, Any]:
        """Fetch priority history bundle."""

    @abstractmethod
    async def fetch_order_book_depth_snapshot(self, symbol: str, *, limit: int=20) -> dict[str, float | None]:
        """Fetch order book depth snapshot."""

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> float | None:
        """Fetch funding rate."""

    @abstractmethod
    async def fetch_premium_index_all(self) -> dict[str, dict[str, float]]:
        """Fetch premium index for all symbols."""

    @abstractmethod
    async def fetch_open_interest(self, symbol: str) -> float | None:
        """Fetch open interest."""

    @abstractmethod
    async def fetch_open_interest_change(self, symbol: str, *, period: str='1h') -> float | None:
        """Fetch open interest change."""

    @abstractmethod
    async def fetch_long_short_ratio(self, symbol: str, *, period: str='1h') -> float | None:
        """Fetch long/short ratio."""

    @abstractmethod
    async def fetch_top_position_ls_ratio(self, symbol: str, *, period: str='1h') -> float | None:
        """Fetch top position long/short ratio."""

    @abstractmethod
    async def fetch_taker_ratio(self, symbol: str, *, period: str='1h') -> float | None:
        """Fetch taker buy/sell volume ratio."""

    @abstractmethod
    async def fetch_global_ls_ratio(self, symbol: str, *, period: str='1h') -> float | None:
        """Fetch global long/short account ratio."""

    @abstractmethod
    async def fetch_funding_rate_history(self, symbol: str, *, limit: int=10) -> list[dict[str, Any]]:
        """Fetch funding rate history."""

    @abstractmethod
    async def fetch_agg_trade_snapshot(self, symbol: str, *, limit: int=100) -> AggTradeSnapshot:
        """Fetch aggregate trade snapshot."""

    @abstractmethod
    async def fetch_agg_trades(self, symbol: str, *, start_time_ms: int, end_time_ms: int, page_limit: int, page_size: int) -> tuple[list[AggTrade], bool]:
        """Fetch aggregate trades."""

    @abstractmethod
    async def fetch_book_ticker(self, symbol: str) -> tuple[float | None, float | None]:
        """Fetch best bid/ask price."""

    @abstractmethod
    async def fetch_symbol_frames(self, symbol: str) -> SymbolFrames:
        """Fetch symbol frames for multiple timeframes and order book context."""

    @abstractmethod
    async def close(self) -> None:
        """Close the client and release resources."""

    @abstractmethod
    def state_snapshot(self) -> dict[str, float | int | str | None]:
        """Get current state snapshot for monitoring."""

    @abstractmethod
    async def preflight_check(self) -> None:
        """Perform preflight checks."""

    @abstractmethod
    def get_cached_klines(self, symbol: str, interval: str, *, limit: int, max_age_s: float | None=None) -> Any:
        """Return cached klines frame if still fresh."""

    @abstractmethod
    async def _fetch_book_ticker_rest_detail(self, symbol: str) -> dict[str, float | None]:
        """Fetch bid/ask detail for a symbol."""

    @abstractmethod
    def get_cached_oi_change(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        pass

    @abstractmethod
    def get_cached_open_interest(self, symbol: str, max_age_s: float=1800.0) -> float | None:
        pass

    @abstractmethod
    def get_cached_ls_ratio(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        pass

    @abstractmethod
    def get_cached_funding_rate(self, symbol: str, max_age_s: float=1800.0) -> float | None:
        pass

    @abstractmethod
    def get_cached_premium_index(self, symbol: str, max_age_s: float=300.0) -> dict[str, float] | None:
        pass

    @abstractmethod
    def get_cached_top_position_ls_ratio(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        pass

    @abstractmethod
    def get_cached_taker_ratio(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        pass

    @abstractmethod
    def get_cached_global_ls_ratio(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        pass

    @abstractmethod
    def get_cached_basis_stats(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> dict[str, float | None] | None:
        pass

    @abstractmethod
    def update_basis_from_websocket(self, symbol: str, mark_price: float, index_price: float | None=None, period: str='5m') -> dict[str, float | None] | None:
        pass

    @abstractmethod
    def get_cached_funding_trend(self, symbol: str, max_age_s: float=1800.0) -> str | None:
        pass

    @abstractmethod
    def get_cached_funding_recent_extreme(self, symbol: str, *, max_age_hours: float=48.0, max_cache_age_s: float=1800.0) -> tuple[float, float] | None:
        pass

    @abstractmethod
    async def fetch_basis(self, symbol: str, *, period: str='1h', limit: int=3) -> float | None:
        pass

    @abstractmethod
    def get_cached_basis(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        pass

LOG = logging.getLogger('bot.market.rest')

class RestHttpMixin(RestCircuitMixin):
    """HTTP session, rate limits, circuit breaker, and public REST calls."""
    _proxy_url: str | None
    _trust_env: bool
    _rest_timeout: float
    _futures_data_limit_per_5m: int
    _rate_limit_pause_until: float
    _futures_data_pause_until: float
    _rate_limit_error_streak: int
    _weight_window_weight: int
    _weight_window_start: float
    _weight_budget: _WeightBudgetManager
    _futures_data_limiter: _SlidingWindowRateLimiter
    _http_session: aiohttp.ClientSession | None
    _last_rest_weight_1m: int | None
    _last_rest_response_time_ms: float | None
    _circuit_failures: dict[str, int]
    _circuit_open_until: dict[str, float]
    _circuit_half_open: set[str]
    _circuit_failure_threshold: int
    _circuit_open_duration_seconds: float
    _critical_operations: set[str]
    _last_endpoint_name: str | None
    _last_endpoint_source: str | None
    _last_endpoint_cache_hit: bool
    _last_endpoint_fallback_used: bool
    _last_endpoint_limiter_wait_ms: float
    _last_endpoint_response_age_s: float | None

    def _endpoint_spec(self, operation: str) -> _PublicEndpointSpec:
        raise NotImplementedError

    def _endpoint_url(self, operation: str) -> str:
        raise NotImplementedError

    def _record_endpoint_snapshot(self, endpoint_name: str, *, source: str, cache_hit: bool, fallback_used: bool, limiter_wait_ms: float=0.0, response_age_s: float | None=None) -> None:
        raise NotImplementedError

    async def _call_public_http_json(self, operation: str, *, params: dict[str, Any] | None=None, symbol: str | None=None) -> Any:
        pool = getattr(self, '_proxy_pool', None)
        failover = bool(getattr(self, '_proxy_failover_enabled', False))
        attempts = len(pool.urls) if pool and failover else 1
        last_error: Exception | None = None
        for attempt in range(max(1, attempts)):
            try:
                return await self._call_public_http_json_attempt(operation, params=params, symbol=symbol)
            except MarketDataUnavailable as exc:
                last_error = exc
                detail = str(exc.detail or '')
                if attempt + 1 < attempts and await self._try_failover_proxy(detail):
                    continue
                raise
            except aiohttp.ClientError as exc:
                last_error = exc
                if attempt + 1 < attempts and is_proxy_transport_error(exc) and await self._try_failover_proxy(str(exc)):
                    continue
                self._record_circuit_failure(operation)
                raise MarketDataUnavailable(operation=operation, detail=f'aiohttp:{exc.__class__.__name__}:{exc}', symbol=symbol) from exc
        if last_error is not None:
            raise last_error
        raise MarketDataUnavailable(operation=operation, detail='proxy_pool_exhausted', symbol=symbol)

    async def _call_public_http_json_attempt(self, operation: str, *, params: dict[str, Any] | None=None, symbol: str | None=None) -> Any:
        """Call a public REST endpoint via aiohttp with the same circuit/rate-limit guards."""
        spec, url, limiter_wait_s = await self._prepare_public_rest_call(operation, params=params, symbol=symbol)

        class _ResponseStub:
            __slots__ = ('headers',)

            def __init__(self, headers: Mapping[str, str]) -> None:
                self.headers = headers
        try:
            async with rest_global_semaphore():
                session = await self._get_http_session()
                request_proxy = aiohttp_request_proxy(session, getattr(self, '_proxy_url', None))
                async with session.get(url, params=params, proxy=request_proxy) as response:
                    headers = response.headers
                    status = int(response.status)
                    if status == 418:
                        self._rate_limit_error_streak += 1
                        retry_after = self._capture_retry_after(headers)
                        self._set_rate_limit_pause(1800.0)
                        LOG.critical('BINANCE IP BAN (418) | retry_after=%s pause=1800s+ streak=%d operation=%s', retry_after, self._rate_limit_error_streak, operation)
                        self._record_circuit_failure(operation)
                        raise MarketDataUnavailable(operation=operation, detail='418 ip ban', symbol=symbol)
                    if status == 429:
                        self._rate_limit_error_streak += 1
                        retry_after_header = self._capture_retry_after(headers, operation=operation)
                        is_ip_limited = bool(_PUBLIC_ENDPOINT_REGISTRY.get(operation, _PublicEndpointSpec('x')).ip_limited)
                        if is_ip_limited:
                            effective_pause = max(60.0, float(retry_after_header or 60))
                            self._set_futures_data_pause(effective_pause)
                            LOG.info('futures-data IP rate limit 429 | operation=%s pause=%.0fs', operation, self._futures_data_pause_until - time.monotonic())
                        else:
                            effective_pause = max(1800.0, float(retry_after_header or 0))
                            self._set_rate_limit_pause(effective_pause)
                            LOG.error('binance rate limited (429) | retry_after_header=%s effective_pause=%.0fs streak=%d operation=%s', retry_after_header, effective_pause, self._rate_limit_error_streak, operation)
                        self._record_circuit_failure(operation)
                        raise MarketDataUnavailable(operation=operation, detail=f'429 rate limited (pause={effective_pause}s)', symbol=symbol)
                    if status < 200 or status >= 300:
                        text = await response.text()
                        detail = text[:240].replace('\n', ' ') if text else f'http={status}'
                        self._rate_limit_error_streak = 0
                        self._record_circuit_failure(operation)
                        raise MarketDataUnavailable(operation=operation, detail=detail, symbol=symbol)
                    try:
                        payload = await response.json()
                    except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
                        self._rate_limit_error_streak = 0
                        self._record_circuit_failure(operation)
                        raise MarketDataUnavailable(operation=operation, detail=f'invalid_json_payload: {exc}', symbol=symbol) from exc
                self._rate_limit_error_streak = 0
                self._capture_response_metadata(_ResponseStub(headers), operation=operation)
                self._track_weight(operation, params)
                self._record_circuit_success(operation)
                self._record_endpoint_snapshot(operation, source=spec.source, cache_hit=False, fallback_used=False, limiter_wait_ms=limiter_wait_s * 1000.0, response_age_s=0.0)
                return payload
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            self._record_circuit_failure(operation)
            if operation in _FALLBACK_TIMEOUT_DEBUG_OPERATIONS:
                log_timeout = LOG.debug
            elif operation in _REST_TIMEOUT_WARNING_OPERATIONS:
                log_timeout = LOG.warning
            else:
                log_timeout = LOG.error
            log_timeout('rest timeout | operation=%s symbol=%s timeout=%.1fs exception=%s', operation, symbol, self._rest_timeout, type(exc).__name__)
            raise MarketDataUnavailable(operation=operation, detail=f'timeout after {self._rest_timeout}s', symbol=symbol) from exc

    async def _prepare_public_rest_call(self, operation: str, *, params: dict[str, Any] | None, symbol: str | None) -> tuple[_PublicEndpointSpec, str, float]:
        spec = self._endpoint_spec(operation)
        url = self._endpoint_url(operation)
        if self._is_circuit_open(operation):
            raise MarketDataUnavailable(operation=operation, detail=f'circuit breaker open for {self._circuit_open_duration_seconds}s', symbol=symbol)
        validate_runtime_public_rest_url(url)
        _validate_rest_params(params)
        limiter_wait_s = 0.0
        if spec.ip_limited:
            limiter_wait_s = await self._futures_data_limiter.acquire(label=operation)
        if spec.ip_limited:
            pause_remaining = self._futures_data_pause_until - time.monotonic()
        else:
            pause_remaining = self._rate_limit_pause_until - time.monotonic()
        if pause_remaining > 0:
            LOG.debug('rate-limit backoff | sleeping=%.1fs operation=%s', pause_remaining, operation)
            await asyncio.sleep(pause_remaining)
            if spec.ip_limited:
                self._futures_data_pause_until = 0.0
        estimated = self._estimate_weight(operation, params)
        weight_wait_s = await self._weight_budget.acquire(weight=estimated, label=operation)
        if weight_wait_s > 0.0:
            limiter_wait_s += weight_wait_s
        self._weight_window_weight = self._weight_budget.used_weight
        return (spec, url, limiter_wait_s)

    @staticmethod
    def _header_value(headers: Any, name: str) -> str | None:
        if not isinstance(headers, Mapping):
            return None
        needle = name.lower()
        for key, value in headers.items():
            if str(key).lower() == needle and value is not None:
                return str(value).strip()
        return None

    def _set_rate_limit_pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._rate_limit_pause_until = max(self._rate_limit_pause_until, time.monotonic() + seconds)

    def _set_futures_data_pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._futures_data_pause_until = max(self._futures_data_pause_until, time.monotonic() + seconds)

    def _uses_futures_data_pause(self, operation: str | None) -> bool:
        return bool(operation and operation in _FUTURES_DATA_REQUEST_LIMITED_OPS)

    def _set_operation_rate_limit_pause(self, operation: str | None, seconds: float) -> None:
        if self._uses_futures_data_pause(operation):
            self._set_futures_data_pause(seconds)
        else:
            self._set_rate_limit_pause(seconds)

    def _capture_retry_after(self, headers: Any, *, operation: str | None=None) -> int | None:
        retry_after_raw = self._header_value(headers, 'Retry-After')
        if retry_after_raw is None:
            return None
        try:
            retry_after = max(0, int(float(retry_after_raw)))
        except (TypeError, ValueError):
            return None
        if retry_after > 0:
            self._set_operation_rate_limit_pause(operation, retry_after)
        return retry_after

    @staticmethod
    def _calculate_backoff(attempt: int, *, base_delay: float=1.0, cap: float=60.0) -> float:
        delay = base_delay * 2 ** max(attempt, 0)
        jitter = random.uniform(0.5, 1.5)
        return float(min(delay * jitter, cap))

    def _estimate_weight(self, operation: str, params: Any | None=None) -> int:
        if operation in {'kline_candlestick_data', 'continuous_kline_candlestick_data', 'mark_price_kline_data', 'index_price_kline_data'}:
            try:
                limit = int((params or {}).get('limit') or _DEFAULT_KLINE_FETCH_LIMIT)
            except (TypeError, ValueError):
                limit = _DEFAULT_KLINE_FETCH_LIMIT
            if limit < 100:
                return 1
            if limit < 500:
                return 2
            if limit <= 1000:
                return 5
            return 10
        if operation == 'order_book_depth':
            try:
                limit = validate_order_book_depth_limit(int((params or {}).get('limit') or _DEFAULT_ORDER_BOOK_DEPTH_LIMIT))
            except (TypeError, ValueError):
                limit = _DEFAULT_ORDER_BOOK_DEPTH_LIMIT
            if limit <= 50:
                return 5
            if limit <= 100:
                return 10
            if limit <= 500:
                return 25
            return 50
        if operation == 'premium_index':
            symbol = (params or {}).get('symbol') if isinstance(params, Mapping) else None
            return 1 if symbol else 10
        return _ENDPOINT_WEIGHTS.get(operation, 10)

    def _track_weight(self, operation: str, _params: Mapping[str, Any] | None=None) -> None:
        """Record the current client-side REST weight estimate."""
        self._weight_window_weight = self._weight_budget.used_weight
        self._weight_window_start = time.monotonic()
        if self._weight_window_weight >= _REST_WEIGHT_HARD_LIMIT:
            LOG.error('client-side weight budget at hard limit | estimated_1m=%d operation=%s', self._weight_window_weight, operation)
        elif self._weight_window_weight >= _REST_WEIGHT_SOFT_LIMIT:
            LOG.info('client-side weight budget elevated | estimated_1m=%d operation=%s', self._weight_window_weight, operation)

    def _capture_response_metadata(self, response: Any, *, operation: str | None=None) -> None:
        """Capture Binance REST response headers used by health telemetry."""
        headers = getattr(response, 'headers', None)
        if not isinstance(headers, Mapping):
            return
        weight_raw = None if operation == 'symbol_order_book_ticker' else self._header_value(headers, 'x-mbx-used-weight-1m')
        response_time_raw = self._header_value(headers, 'x-response-time')
        try:
            if weight_raw is not None:
                self._last_rest_weight_1m = int(weight_raw)
        except (TypeError, ValueError):
            self._last_rest_weight_1m = None
        try:
            if response_time_raw is not None:
                self._last_rest_response_time_ms = float(response_time_raw.rstrip('ms'))
        except (TypeError, ValueError):
            self._last_rest_response_time_ms = None

    async def _get_http_session(self) -> aiohttp.ClientSession:
        session = self._http_session
        if session is None or session.closed:
            timeout = aiohttp.ClientTimeout(total=self._rest_timeout)
            self._http_session = create_aiohttp_session(proxy_url=getattr(self, '_proxy_url', None), trust_env=bool(getattr(self, '_trust_env', True)), timeout=timeout, connector_limit=_HTTP_CONNECTOR_LIMIT)
        session = self._http_session
        assert session is not None
        return session

    async def close(self) -> None:
        """Close aiohttp session."""
        await close_aiohttp_session(self._http_session)
        self._http_session = None

    def state_snapshot(self) -> dict[str, float | int | str | None]:
        now = time.monotonic()
        open_circuits = sum((1 for v in self._circuit_open_until.values() if now < float(v)))
        rest_pause_remaining = max(0.0, self._rate_limit_pause_until - now)
        futures_data_pause_remaining = max(0.0, self._futures_data_pause_until - now)
        return {'rest_weight_1m': float(self._last_rest_weight_1m) if self._last_rest_weight_1m is not None else 0.0, 'rest_response_time_ms': float(self._last_rest_response_time_ms) if self._last_rest_response_time_ms is not None else 0.0, 'circuit_breakers_open': int(open_circuits), 'circuit_failure_counts': int(sum(self._circuit_failures.values())), 'endpoint_name': str(self._last_endpoint_name or ''), 'source': str(self._last_endpoint_source or ''), 'cache_hit': float(int(bool(self._last_endpoint_cache_hit))), 'fallback_used': float(int(bool(self._last_endpoint_fallback_used))), 'limiter_wait_ms': float(self._last_endpoint_limiter_wait_ms) if self._last_endpoint_limiter_wait_ms is not None else 0.0, 'response_age_s': float(self._last_endpoint_response_age_s) if self._last_endpoint_response_age_s is not None else 0.0, 'futures_data_limit_per_5m': int(self._futures_data_limit_per_5m), 'rest_rate_limit_pause_remaining_s': float(rest_pause_remaining), 'futures_data_pause_remaining_s': float(futures_data_pause_remaining)}

class BinanceClientImpl(RestHttpMixin, BinanceClient):
    """Production implementation of Binance public REST client."""

    def __init__(self, *, ws_manager: Any=None, rest_timeout_seconds: float=20.0, futures_data_request_limit_per_5m: int=_FUTURES_DATA_IP_LIMIT_DEFAULT, proxy_url: str | None=None, trust_env: bool=True, network: NetworkConfig | None=None) -> None:
        net = network or NetworkConfig(proxy_url=proxy_url, trust_env=trust_env)
        self._trust_env = net.trust_env
        self._proxy_failover_enabled = bool(net.failover_enabled)
        urls = net.effective_proxy_urls()
        if not urls and net.trust_env:
            env_only = resolve_proxy_url(config_url=None, trust_env=True)
            if env_only:
                urls = [env_only]
        self._proxy_pool = ProxyPool.from_urls(urls, cooldown_seconds=net.failover_cooldown_seconds)
        self._proxy_url = self._proxy_pool.current() if self._proxy_pool is not None else resolve_proxy_url(config_url=proxy_url, trust_env=trust_env)
        if self._proxy_url:
            apply_proxy_env(self._proxy_url)
        if self._proxy_pool is not None:
            LOG.info('proxy pool ready | active=%s endpoints=%d failover=%s', mask_proxy_url(self._proxy_url or ''), len(self._proxy_pool.urls), self._proxy_failover_enabled)
        self._rest_timeout = rest_timeout_seconds
        self._futures_data_limit_per_5m = max(30, min(int(futures_data_request_limit_per_5m), _FUTURES_DATA_IP_LIMIT_OFFICIAL_MAX))
        self.client: Any = None
        self._exchange_info_cache: tuple[float, list[SymbolMeta]] | None = None
        self._ticker_24h_cache: tuple[float, list[dict[str, float | str]]] | None = None
        self._premium_index_all_cache: tuple[float, dict[str, dict[str, float]]] | None = None
        self._funding_rate_cache: dict[str, tuple[float, float]] = {}
        self._open_interest_cache: dict[str, tuple[float, float]] = {}
        self._open_interest_change_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._long_short_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._taker_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._global_ls_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._top_position_ls_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._funding_history_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._basis_cache: dict[tuple[str, str], tuple[float, float | None]] = {}
        self._basis_stats_cache: dict[tuple[str, str], tuple[float, dict[str, float | None]]] = {}
        self._basis_ws_history: dict[tuple[str, str], deque[tuple[float, float]]] = {}
        self._order_book_depth_cache: dict[tuple[str, int], tuple[float, dict[str, float | None]]] = {}
        self._ws: Any = ws_manager
        self._last_rest_weight_1m: int | None = None
        self._last_rest_response_time_ms: float | None = None
        self._rate_limit_pause_until = 0.0
        self._futures_data_pause_until = 0.0
        self._rate_limit_error_streak = 0
        self._weight_window_weight: int = 0
        self._weight_window_start: float = 0.0
        self._weight_budget = _WeightBudgetManager(max_weight=_REST_WEIGHT_PACE_LIMIT, window_seconds=60.0)
        self._futures_data_limiter = _SlidingWindowRateLimiter(max_requests=self._futures_data_limit_per_5m, window_seconds=_FUTURES_DATA_IP_LIMIT_WINDOW_S)
        self._http_session: aiohttp.ClientSession | None = None
        self._klines_cache: dict[tuple[str, str, int], tuple[float, Any]] = {}
        self._klines_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        self._derived_klines_cache: dict[tuple[str, str, str, int], tuple[float, Any]] = {}
        self._derived_klines_locks: dict[tuple[str, str, str, int], asyncio.Lock] = {}
        self._circuit_failures: dict[str, int] = {}
        self._circuit_open_until: dict[str, float] = {}
        self._circuit_half_open: set[str] = set()
        self._circuit_failure_threshold = 3
        self._circuit_open_duration_seconds = 30.0
        self._critical_operations = {'kline_candlestick_data', 'symbol_order_book_ticker', 'order_book_depth', 'exchange_information'}
        self._last_endpoint_name: str | None = None
        self._last_endpoint_source: str | None = None
        self._last_endpoint_cache_hit: bool = False
        self._last_endpoint_fallback_used: bool = False
        self._last_endpoint_limiter_wait_ms: float = 0.0
        self._last_endpoint_response_age_s: float | None = None

    async def _apply_active_proxy(self, url: str | None) -> None:
        self._proxy_url = url
        if url:
            apply_proxy_env(url)
        await close_aiohttp_session(self._http_session)
        self._http_session = None
        ws = self._ws
        if ws is not None and hasattr(ws, 'update_proxy_url'):
            ws.update_proxy_url(url, trust_env=self._trust_env)

    async def _try_failover_proxy(self, reason: str) -> bool:
        if not self._proxy_failover_enabled:
            return False
        pool = self._proxy_pool
        if pool is None or not pool.has_alternatives():
            return False
        nxt = pool.rotate_after_failure(self._proxy_url, reason)
        if not nxt:
            return False
        await self._apply_active_proxy(nxt)
        return True

    def proxy_pool_snapshot(self) -> dict[str, object]:
        pool = self._proxy_pool
        if pool is None:
            return {'enabled': False, 'active': mask_proxy_url(self._proxy_url or '')}
        snap = pool.snapshot()
        snap['enabled'] = True
        snap['failover_enabled'] = self._proxy_failover_enabled
        return snap

    async def fetch_exchange_symbols(self) -> list[SymbolMeta]:
        now = time.monotonic()
        if self._exchange_info_cache is not None:
            cached_at, rows = self._exchange_info_cache
            if now - cached_at < 3600:
                self._record_endpoint_snapshot('exchange_information', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached_at)
                return rows
        try:
            payload = await self._call_public_http_json('exchange_information')
        except MarketDataUnavailable as exc:
            if self._exchange_info_cache is not None:
                cached_at, rows = self._exchange_info_cache
                self._record_endpoint_snapshot('exchange_information', source='rest', cache_hit=True, fallback_used=True, response_age_s=now - cached_at)
                LOG.info('fetch_exchange_symbols failed, using stale cache | age=%.0fs error=%s', now - cached_at, exc.detail)
                return rows
            raise
        symbols = payload.get('symbols', []) if isinstance(payload, dict) else getattr(payload, 'symbols', [])
        rows = [SymbolMeta(symbol=str(item.get('symbol', '')) if isinstance(item, dict) else str(getattr(item, 'symbol', '')), base_asset=str(item.get('baseAsset', '')) if isinstance(item, dict) else str(getattr(item, 'base_asset', '')), quote_asset=str(item.get('quoteAsset', '')) if isinstance(item, dict) else str(getattr(item, 'quote_asset', '')), contract_type=str(item.get('contractType', '')) if isinstance(item, dict) else str(getattr(item, 'contract_type', '')), status=str(item.get('status', '')) if isinstance(item, dict) else str(getattr(item, 'status', '')), onboard_date_ms=int(item.get('onboardDate', 0) or 0) if isinstance(item, dict) else int(getattr(item, 'onboard_date', 0) or 0)) for item in symbols]
        self._exchange_info_cache = (now, rows)
        return rows

    async def fetch_ticker_24h(self) -> list[dict[str, float | str]]:
        now = time.monotonic()
        if self._ticker_24h_cache is not None:
            cached_at, rows = self._ticker_24h_cache
            if now - cached_at < 300:
                self._record_endpoint_snapshot('ticker24hr_price_change_statistics', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached_at)
                return rows
        try:
            payload = await self._call_public_http_json('ticker24hr_price_change_statistics')
        except MarketDataUnavailable as exc:
            if self._ticker_24h_cache is not None:
                cached_at, stale_rows = self._ticker_24h_cache
                stale_age = now - cached_at
                self._record_endpoint_snapshot('ticker24hr_price_change_statistics', source='rest', cache_hit=True, fallback_used=True, response_age_s=stale_age)
                LOG.info('fetch_ticker_24h failed, using stale cache | age=%.0fs | error=%s', stale_age, exc.detail)
                return stale_rows
            raise
        new_rows: list[dict[str, float | str]] = []
        for item in payload if isinstance(payload, list) else []:
            if isinstance(item, dict):
                symbol = str(item.get('symbol', '')).strip().upper()
                last_price = _safe_float(item.get('lastPrice') or item.get('last_price'))
                price_change_percent = _safe_float(item.get('priceChangePercent') or item.get('price_change_percent'))
                quote_volume = _safe_float(item.get('quoteVolume') or item.get('quote_volume'))
                trade_count = _safe_float(item.get('count') or item.get('trade_count'))
                if not symbol or last_price <= 0.0 or quote_volume <= 0.0:
                    continue
                new_rows.append({'symbol': symbol, 'last_price': last_price, 'price_change_percent': price_change_percent, 'quote_volume': quote_volume, 'trade_count': trade_count})
            else:
                symbol = str(getattr(item, 'symbol', '')).strip().upper()
                last_price = _safe_float(getattr(item, 'last_price', None) or getattr(item, 'lastPrice', None))
                price_change_percent = _safe_float(getattr(item, 'price_change_percent', 0) or getattr(item, 'priceChangePercent', 0))
                quote_volume = _safe_float(getattr(item, 'quote_volume', None) or getattr(item, 'quoteVolume', None))
                trade_count = _safe_float(getattr(item, 'count', None) or getattr(item, 'trade_count', None))
                if not symbol or last_price <= 0.0 or quote_volume <= 0.0:
                    continue
                new_rows.append({'symbol': symbol, 'last_price': last_price, 'price_change_percent': price_change_percent, 'quote_volume': quote_volume, 'trade_count': trade_count})
        self._ticker_24h_cache = (now, new_rows)
        return new_rows

    def _is_cache_valid(self, cache_entry: tuple[float, Any] | None, ttl_seconds: int) -> bool:
        if cache_entry is None:
            return False
        cached_at, _ = cache_entry
        return time.monotonic() - cached_at < ttl_seconds

    async def _fetch_derived_klines_uncached(self, kind: str, symbol: str, interval: str, *, limit: int) -> Any:
        validate_symbol(symbol)
        validate_interval(interval)
        validate_limit(limit)
        if kind == 'continuous':
            contract_type = 'PERPETUAL'
            assert contract_type == 'PERPETUAL', f'Only PERPETUAL contracts supported, got {contract_type}'
            try:
                rows = await self._call_public_http_json('continuous_kline_candlestick_data', params={'pair': symbol, 'contractType': contract_type, 'interval': interval, 'limit': limit}, symbol=symbol)
            except RuntimeError as exc:
                message = str(exc)
                if '"code":-4104' in message or 'Invalid contract type' in message:
                    LOG.debug('continuous klines unsupported for symbol | symbol=%s interval=%s', symbol, interval)
                    return pl.DataFrame()
                raise
        elif kind == 'mark':
            rows = await self._call_public_http_json('mark_price_kline_data', params={'symbol': symbol, 'interval': interval, 'limit': limit}, symbol=symbol)
        elif kind == 'index':
            rows = await self._call_public_http_json('index_price_kline_data', params={'pair': symbol, 'interval': interval, 'limit': limit}, symbol=symbol)
        else:
            msg = f'unsupported derived kline kind: {kind!r}'
            raise ValueError(msg)
        return _drop_incomplete_ohlcv_tail(_klines_to_frame(rows), interval)

    async def _fetch_derived_klines_cached(self, kind: str, symbol: str, interval: str, *, limit: int) -> Any:
        validate_symbol(symbol)
        validate_interval(interval)
        validate_limit(limit)
        cache_ttl_key = {'continuous': 'continuous_klines', 'mark': 'mark_price_klines', 'index': 'index_price_klines'}.get(kind)
        if cache_ttl_key is None:
            msg = f'unsupported derived kline kind: {kind!r}'
            raise ValueError(msg)
        key = (kind, symbol, interval, int(limit))
        ttl = int(_CACHE_TTL.get(cache_ttl_key, 900))
        now = time.monotonic()
        cached = self._derived_klines_cache.get(key)
        if cached is not None and now - cached[0] < ttl:
            self._record_endpoint_snapshot(f'{kind}_klines', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1]
        lock = self._derived_klines_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._derived_klines_locks[key] = lock
        try:
            async with lock:
                now = time.monotonic()
                cached = self._derived_klines_cache.get(key)
                if cached is not None and now - cached[0] < ttl:
                    self._record_endpoint_snapshot(f'{kind}_klines', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
                    return cached[1]
                frame = await self._fetch_derived_klines_uncached(kind, symbol, interval, limit=limit)
                self._derived_klines_cache[key] = (time.monotonic(), frame)
                return frame
        finally:
            active_lock = self._derived_klines_locks.get(key)
            if active_lock is lock and (not lock.locked()):
                self._derived_klines_locks.pop(key, None)

    async def fetch_continuous_klines(self, symbol: str, interval: str, *, limit: int=500) -> Any:
        """Fetch public continuous USD-M klines for backtest-stable history."""
        return await self._fetch_derived_klines_cached('continuous', symbol, interval, limit=limit)

    async def fetch_mark_price_klines(self, symbol: str, interval: str, *, limit: int=500) -> Any:
        """Fetch public mark-price klines for premium/basis analytics."""
        return await self._fetch_derived_klines_cached('mark', symbol, interval, limit=limit)

    async def fetch_index_price_klines(self, symbol: str, interval: str, *, limit: int=500) -> Any:
        """Fetch public index-price klines for spot/futures divergence analytics."""
        return await self._fetch_derived_klines_cached('index', symbol, interval, limit=limit)

    async def fetch_priority_history_bundle(self, symbol: str, *, intervals: tuple[str, ...]=('15m', '1h', '4h'), limit: int=300) -> dict[str, Any]:
        validate_symbol(symbol)
        frames: dict[str, Any] = {}
        for interval in intervals:
            validate_interval(interval)
            settled_limit = max(1, min(int(limit), 1500))
            fetches = await asyncio.gather(self.fetch_klines_cached(symbol, interval, limit=settled_limit), self.fetch_continuous_klines(symbol, interval, limit=settled_limit), self.fetch_mark_price_klines(symbol, interval, limit=settled_limit), self.fetch_index_price_klines(symbol, interval, limit=settled_limit), return_exceptions=True)
            for suffix, result in zip(('trade', 'continuous', 'mark', 'index'), fetches, strict=True):
                key = f'{interval}:{suffix}'
                if isinstance(result, Exception):
                    LOG.info('priority history fetch skipped | symbol=%s interval=%s kind=%s error=%s', symbol, interval, suffix, result)
                    continue
                frames[key] = result
        return frames

    def get_cached_klines(self, symbol: str, interval: str, *, limit: int, max_age_s: float | None=None) -> Any:
        key = (symbol, interval, int(limit))
        cached = self._klines_cache.get(key)
        if cached is None:
            return None
        cached_at, frame = cached
        ttl = float(max_age_s if max_age_s is not None else _CACHE_TTL.get(f'klines_{interval}', 60))
        if time.monotonic() - cached_at > ttl:
            return None
        return frame

    async def fetch_order_book_depth_snapshot(self, symbol: str, *, limit: int=_DEFAULT_ORDER_BOOK_DEPTH_LIMIT) -> dict[str, float | None]:
        validate_symbol(symbol)
        limit = validate_order_book_depth_limit(limit)
        key = (symbol, limit)
        now = time.monotonic()
        cached = self._order_book_depth_cache.get(key)
        ttl = int(_CACHE_TTL['order_book_depth'])
        if cached is not None and now - cached[0] < ttl:
            self._record_endpoint_snapshot('order_book_depth', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return dict(cached[1])
        payload = await self._call_public_http_json('order_book_depth', params={'symbol': symbol, 'limit': limit}, symbol=symbol)
        if not isinstance(payload, Mapping):
            raise MarketDataUnavailable(operation='order_book_depth', detail=f'unexpected payload type: {type(payload).__name__}', symbol=symbol)
        bids = _parse_depth_levels(payload.get('bids'), reverse=True)
        asks = _parse_depth_levels(payload.get('asks'), reverse=False)
        if not bids or not asks:
            raise MarketDataUnavailable(operation='order_book_depth', detail='empty order book levels', symbol=symbol)
        last_update_raw = payload.get('lastUpdateId') or payload.get('last_update_id')
        try:
            last_update_id = float(last_update_raw) if last_update_raw is not None else None
        except (TypeError, ValueError):
            last_update_id = None
        snapshot: dict[str, float | None] = {'bid_price': bids[0][0], 'ask_price': asks[0][0], 'bid_qty': sum((qty for _price, qty in bids)), 'ask_qty': sum((qty for _price, qty in asks)), 'last_update_id': last_update_id}
        self._order_book_depth_cache[key] = (time.monotonic(), snapshot)
        return dict(snapshot)

    async def _fetch_order_book_context_rest_detail(self, symbol: str) -> dict[str, float | None]:
        try:
            return await self.fetch_order_book_depth_snapshot(symbol, limit=_DEFAULT_ORDER_BOOK_DEPTH_LIMIT)
        except MarketDataUnavailable as exc:
            LOG.info('order book depth unavailable, falling back to book ticker | symbol=%s detail=%s', symbol, exc.detail)
            return await self._fetch_book_ticker_rest_detail(symbol)

    async def _fetch_book_ticker_rest_detail(self, symbol: str) -> dict[str, float | None]:
        validate_symbol(symbol)
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                payload = await self._call_public_http_json('symbol_order_book_ticker', params={'symbol': symbol}, symbol=symbol)
                if isinstance(payload, Mapping):
                    bid_raw = payload.get('bidPrice') or payload.get('bid_price')
                    ask_raw = payload.get('askPrice') or payload.get('ask_price')
                    bid_qty_raw = payload.get('bidQty') or payload.get('bid_qty')
                    ask_qty_raw = payload.get('askQty') or payload.get('ask_qty')
                else:
                    bid_raw = getattr(payload, 'bid_price', None)
                    ask_raw = getattr(payload, 'ask_price', None)
                    bid_qty_raw = getattr(payload, 'bid_qty', None)
                    ask_qty_raw = getattr(payload, 'ask_qty', None)
                float(bid_raw) if bid_raw is not None else None
                float(ask_raw) if ask_raw is not None else None
                float(bid_qty_raw) if bid_qty_raw is not None else None
                float(ask_qty_raw) if ask_qty_raw is not None else None
            except MarketDataUnavailable as exc:
                detail = (exc.detail or '').lower()
                if attempt < max_attempts and 'timeout' in detail:
                    backoff = min(2.0, 0.5 * 2 ** (attempt - 1)) * random.uniform(0.9, 1.1)
                    LOG.info('book ticker retry | symbol=%s attempt=%d/%d backoff=%.2fs detail=%s', symbol, attempt, max_attempts, backoff, detail)
                    await asyncio.sleep(backoff)
                    continue
                LOG.info('book ticker unavailable, returning empty prices | symbol=%s detail=%s', symbol, detail)
                return {'bid_price': None, 'ask_price': None, 'bid_qty': None, 'ask_qty': None}
        return {'bid_price': None, 'ask_price': None, 'bid_qty': None, 'ask_qty': None}

    async def _fetch_book_ticker_rest(self, symbol: str) -> tuple[float | None, float | None]:
        detail = await self._fetch_book_ticker_rest_detail(symbol)
        return (detail.get('bid_price'), detail.get('ask_price'))

    async def _fetch_agg_trade_snapshot_rest(self, symbol: str, *, limit: int=100) -> AggTradeSnapshot:
        validate_symbol(symbol)
        validate_limit(limit, max_val=1000)
        payload = await self._call_public_http_json('compressed_aggregate_trades_list', params={'symbol': symbol, 'limit': limit}, symbol=symbol)
        buy_qty = 0.0
        sell_qty = 0.0
        trade_count = 0
        payload_rows = cast('list[Any]', payload)
        for item in payload_rows:
            row = _coerce_rest_row(item)
            qty = float(row.get('q') or 0.0)
            is_buyer_maker = bool(row.get('m'))
            trade_count += 1
            if is_buyer_maker:
                sell_qty += qty
            else:
                buy_qty += qty
        total_qty = buy_qty + sell_qty
        delta_ratio = None
        if total_qty > 0:
            delta_ratio = (buy_qty - sell_qty) / total_qty
        return AggTradeSnapshot(symbol=symbol, trade_count=trade_count, buy_qty=buy_qty, sell_qty=sell_qty, delta_ratio=delta_ratio)

    async def fetch_book_ticker(self, symbol: str) -> tuple[float | None, float | None]:
        if self._ws is not None:
            cached = await self._ws.get_book_ticker(symbol)
            if cached is not None:
                return cached
        return await self._fetch_book_ticker_rest(symbol)

    async def fetch_agg_trade_snapshot(self, symbol: str, *, limit: int=100) -> AggTradeSnapshot:
        if self._ws is not None:
            snapshot = self._ws.get_agg_trade_snapshot(symbol)
            if snapshot is not None:
                return snapshot
        return await self._fetch_agg_trade_snapshot_rest(symbol, limit=limit)

    async def fetch_agg_trades(self, symbol: str, *, start_time_ms: int, end_time_ms: int, page_limit: int, page_size: int) -> tuple[list[AggTrade], bool]:
        validate_symbol(symbol)
        rows: list[AggTrade] = []
        pages = 0
        complete = True
        window_start_ms = max(int(start_time_ms), 0)
        final_end_ms = min(max(int(end_time_ms), 0), int(time.time() * 1000))
        max_window_ms = 3599000
        while pages < page_limit and window_start_ms <= final_end_ms:
            window_end_ms = min(window_start_ms + max_window_ms, final_end_ms)
            next_from_id: int | None = None
            while pages < page_limit:
                kwargs: dict[str, Any] = {'symbol': symbol, 'limit': page_size}
                if next_from_id is None:
                    kwargs['startTime'] = window_start_ms
                    kwargs['endTime'] = window_end_ms
                else:
                    kwargs['fromId'] = next_from_id
                payload = await self._call_public_http_json('compressed_aggregate_trades_list', params=kwargs, symbol=symbol)
                batch: list[AggTrade] = []
                payload_rows = cast('list[Any]', payload)
                for item in payload_rows:
                    row = _coerce_rest_row(item)
                    trade_time_ms = int(row.get('T') or 0)
                    if trade_time_ms < start_time_ms:
                        continue
                    if trade_time_ms > end_time_ms:
                        continue
                    trade_id = int(row.get('a') or 0)
                    batch.append(AggTrade(symbol=symbol, trade_id=trade_id, price=float(row.get('p') or 0.0), quantity=float(row.get('q') or 0.0), trade_time_ms=trade_time_ms, is_buyer_maker=bool(row.get('m'))))
                if not payload_rows:
                    break
                rows.extend(batch)
                pages += 1
                last_row = _coerce_rest_row(payload_rows[-1])
                next_from_id = int(last_row.get('a') or 0) + 1
                last_time_ms = int(last_row.get('T') or 0)
                if len(payload_rows) < page_size or last_time_ms >= window_end_ms:
                    break
                await asyncio.sleep(0.05)
            if pages >= page_limit and window_end_ms < final_end_ms:
                complete = False
                break
            window_start_ms = window_end_ms + 1
        deduped: dict[int, AggTrade] = {}
        for item in rows:
            deduped[item.trade_id] = item
        sorted_rows = sorted(deduped.values(), key=lambda item: (item.trade_time_ms, item.trade_id))
        if sorted_rows and sorted_rows[-1].trade_time_ms < end_time_ms and (pages >= page_limit):
            complete = False
        return (sorted_rows, complete)

    async def fetch_funding_rate(self, symbol: str) -> float | None:
        validate_symbol(symbol)
        now = time.monotonic()
        cached = self._funding_rate_cache.get(symbol)
        if cached is not None and now - cached[0] < 300:
            self._record_endpoint_snapshot('premium_index', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1]
        try:
            payload = await self._call_public_http_json('premium_index', params={'symbol': symbol}, symbol=symbol)
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot('premium_index', source='rest', cache_hit=True, fallback_used=True, response_age_s=now - cached[0])
                return cached[1]
            raise
        value_raw = payload.get('lastFundingRate') or payload.get('last_funding_rate')
        value = float(value_raw) if value_raw is not None else None
        if value is not None:
            self._funding_rate_cache[symbol] = (now, value)
        return value

    async def fetch_premium_index_all(self) -> dict[str, dict[str, float]]:
        now = time.monotonic()
        cached = self._premium_index_all_cache
        if cached is not None and now - cached[0] < 300:
            self._record_endpoint_snapshot('premium_index', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1]
        try:
            payload = await self._call_public_http_json('premium_index')
        except MarketDataUnavailable as exc:
            if cached is not None:
                self._record_endpoint_snapshot('premium_index', source='rest', cache_hit=True, fallback_used=True, response_age_s=now - cached[0])
                LOG.info('fetch_premium_index_all failed, using stale cache | age=%.0fs error=%s', now - cached[0], exc.detail)
                return cached[1]
            raise
        rows: dict[str, dict[str, float]] = {}
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, Mapping):
                continue
            symbol = str(item.get('symbol') or '').strip().upper()
            if not symbol:
                continue
            funding_rate = _safe_float(item.get('lastFundingRate'))
            mark_price = _safe_float(item.get('markPrice'))
            index_price = _safe_float(item.get('indexPrice'))
            basis_pct = (mark_price - index_price) / index_price * 100.0 if mark_price > 0.0 and index_price > 0.0 else 0.0
            rows[symbol] = {'funding_rate': funding_rate, 'basis_pct': basis_pct, 'mark_price': mark_price, 'index_price': index_price}
            self._funding_rate_cache[symbol] = (now, funding_rate)
            self._basis_cache[symbol, '1h'] = (now, basis_pct)
        self._premium_index_all_cache = (now, rows)
        return rows

    async def fetch_open_interest(self, symbol: str) -> float | None:
        validate_symbol(symbol)
        now = time.monotonic()
        cached = self._open_interest_cache.get(symbol)
        if self._is_cache_valid(cached, _CACHE_TTL['open_interest']):
            assert cached is not None
            self._record_endpoint_snapshot('open_interest', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1] if cached else None
        value: float | None = None
        try:
            payload = await self._call_public_http_json('open_interest', params={'symbol': symbol}, symbol=symbol)
            row = payload.model_dump() if hasattr(payload, 'model_dump') else dict(payload)
            value_raw = row.get('open_interest') or row.get('openInterest')
            value = float(value_raw) if value_raw is not None else None
            if value is not None:
                self._open_interest_cache[symbol] = (now, value)
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot('open_interest', source='rest', cache_hit=True, fallback_used=True, response_age_s=now - cached[0])
                LOG.debug('OI graceful degradation | symbol=%s using stale cache', symbol)
                return cached[1]
            return None
        else:
            return value

    async def fetch_open_interest_change(self, symbol: str, *, period: str='1h') -> float | None:
        validate_symbol(symbol)
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._open_interest_change_cache.get(cache_key)
        if self._is_cache_valid(cached, _CACHE_TTL['open_interest_change']):
            assert cached is not None
            self._record_endpoint_snapshot('open_interest_statistics', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1] if cached else None
        change: float | None = None
        try:
            payload = await self._call_public_http_json('open_interest_statistics', params={'symbol': symbol, 'period': period, 'limit': 2}, symbol=symbol)
            if not payload:
                return None
            rows = [item.model_dump() if hasattr(item, 'model_dump') else dict(item) for item in payload]
            rows.sort(key=lambda row: int(row.get('timestamp') or 0))
            if len(rows) < 2:
                return None
            prev_raw = rows[-2].get('sumOpenInterest') or rows[-2].get('sum_open_interest')
            curr_raw = rows[-1].get('sumOpenInterest') or rows[-1].get('sum_open_interest')
            prev = float(prev_raw) if prev_raw is not None else 0.0
            curr = float(curr_raw) if curr_raw is not None else 0.0
            if prev <= 0.0:
                return None
            change = curr / prev - 1.0
            self._open_interest_change_cache[cache_key] = (now, change)
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot('open_interest_statistics', source='rest', cache_hit=True, fallback_used=True, response_age_s=now - cached[0])
                LOG.debug('OI change graceful degradation | symbol=%s period=%s using stale cache', symbol, period)
                return cached[1]
            return None
        else:
            return change

    async def fetch_long_short_ratio(self, symbol: str, *, period: str='1h') -> float | None:
        validate_symbol(symbol)
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._long_short_ratio_cache.get(cache_key)
        if self._is_cache_valid(cached, _CACHE_TTL['long_short_ratio']):
            assert cached is not None
            self._record_endpoint_snapshot('top_trader_long_short_ratio_accounts', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1] if cached else None
        value: float | None = None
        try:
            payload = await self._call_public_http_json('top_trader_long_short_ratio_accounts', params={'symbol': symbol, 'period': period, 'limit': 1}, symbol=symbol)
            if not payload:
                return None
            item = payload[0]
            row = item.model_dump() if hasattr(item, 'model_dump') else dict(item)
            value_raw = row.get('longShortRatio') or row.get('long_short_ratio')
            value = float(value_raw) if value_raw is not None else None
            if value is not None:
                self._long_short_ratio_cache[cache_key] = (now, value)
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot('top_trader_long_short_ratio_accounts', source='rest', cache_hit=True, fallback_used=True, response_age_s=now - cached[0])
                LOG.debug('L/S ratio graceful degradation | symbol=%s period=%s using stale cache', symbol, period)
                return cached[1]
            return None
        else:
            return value

    async def fetch_top_position_ls_ratio(self, symbol: str, *, period: str='1h') -> float | None:
        validate_symbol(symbol)
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._top_position_ls_ratio_cache.get(cache_key)
        if self._is_cache_valid(cached, _CACHE_TTL['long_short_ratio']):
            assert cached is not None
            self._record_endpoint_snapshot('top_trader_long_short_ratio_positions', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1]
        value: float | None = None
        try:
            payload = await self._call_public_http_json('top_trader_long_short_ratio_positions', params={'symbol': symbol, 'period': period, 'limit': 1}, symbol=symbol)
            if not payload:
                return None
            item = payload[0]
            row = item.model_dump() if hasattr(item, 'model_dump') else dict(item)
            value_raw = row.get('longShortRatio') or row.get('long_short_ratio')
            value = float(value_raw) if value_raw is not None else None
            if value is not None:
                self._top_position_ls_ratio_cache[cache_key] = (now, value)
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot('top_trader_long_short_ratio_positions', source='rest', cache_hit=True, fallback_used=True, response_age_s=now - cached[0])
                return cached[1]
            return None
        else:
            return value

    async def fetch_taker_ratio(self, symbol: str, *, period: str='1h') -> float | None:
        validate_symbol(symbol)
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._taker_ratio_cache.get(cache_key)
        if cached is not None and now - cached[0] < 1200:
            self._record_endpoint_snapshot('taker_long_short_ratio', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1]
        try:
            payload = await self._call_public_http_json('taker_long_short_ratio', params={'symbol': symbol, 'period': period, 'limit': 1}, symbol=symbol)
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot('taker_long_short_ratio', source='rest', cache_hit=True, fallback_used=True, response_age_s=now - cached[0])
                return cached[1]
            return None
        if not payload:
            return None
        item = payload[0] if isinstance(payload, list) else payload
        raw = item.get('buySellRatio') or item.get('buy_sell_ratio')
        value = float(raw) if raw is not None else None
        if value is not None:
            self._taker_ratio_cache[cache_key] = (now, value)
        return value

    async def fetch_global_ls_ratio(self, symbol: str, *, period: str='1h') -> float | None:
        validate_symbol(symbol)
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._global_ls_ratio_cache.get(cache_key)
        if cached is not None and now - cached[0] < 1200:
            self._record_endpoint_snapshot('global_long_short_account_ratio', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1]
        try:
            payload = await self._call_public_http_json('global_long_short_account_ratio', params={'symbol': symbol, 'period': period, 'limit': 1}, symbol=symbol)
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot('global_long_short_account_ratio', source='rest', cache_hit=True, fallback_used=True, response_age_s=now - cached[0])
                return cached[1]
            return None
        if not payload:
            return None
        item = payload[0] if isinstance(payload, list) else payload
        raw = item.get('longShortRatio') or item.get('long_short_ratio')
        value = float(raw) if raw is not None else None
        if value is not None:
            self._global_ls_ratio_cache[cache_key] = (now, value)
        return value

    async def fetch_funding_rate_history(self, symbol: str, *, limit: int=10) -> list[dict[str, Any]]:
        validate_symbol(symbol)
        validate_limit(limit, max_val=100)
        now = time.monotonic()
        cached = self._funding_history_cache.get(symbol)
        if cached is not None and now - cached[0] < 900:
            self._record_endpoint_snapshot('funding_rate_history', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1]
        try:
            payload = await self._call_public_http_json('funding_rate_history', params={'symbol': symbol, 'limit': limit}, symbol=symbol)
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot('funding_rate_history', source='rest', cache_hit=True, fallback_used=True, response_age_s=now - cached[0])
                return cached[1]
            return []
        if not isinstance(payload, list):
            return []
        rows = []
        for item in payload:
            try:
                rows.append({'fundingTime': int(item.get('fundingTime') or 0), 'fundingRate': float(item.get('fundingRate') or 0.0), 'markPrice': float(item.get('markPrice') or 0.0)})
            except (TypeError, ValueError):
                continue
        rows.sort(key=lambda r: r['fundingTime'])
        self._funding_history_cache[symbol] = (now, rows)
        return rows

    async def _fetch_symbol_frames_rest(self, symbol: str) -> SymbolFrames:
        frame_4h, frame_1h, frame_15m, frame_5m, book_context = await asyncio.gather(self.fetch_klines_cached(symbol, '4h', limit=_DEFAULT_KLINE_FETCH_LIMIT), self.fetch_klines_cached(symbol, '1h', limit=_DEFAULT_KLINE_FETCH_LIMIT), self.fetch_klines_cached(symbol, '15m', limit=_DEFAULT_KLINE_FETCH_LIMIT), self.fetch_klines_cached(symbol, '5m', limit=_DEFAULT_KLINE_FETCH_LIMIT), self._fetch_order_book_context_rest_detail(symbol))
        return SymbolFrames(symbol=symbol, df_1h=frame_1h, df_15m=frame_15m, bid_price=book_context.get('bid_price'), ask_price=book_context.get('ask_price'), df_5m=frame_5m, df_4h=frame_4h, bid_qty=book_context.get('bid_qty'), ask_qty=book_context.get('ask_qty'))

    async def preflight_check(self) -> None:
        await self.fetch_exchange_symbols()
        await self.fetch_ticker_24h()

    async def fetch_klines(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        validate_symbol(symbol)
        validate_interval(interval)
        validate_limit(limit)
        rows = await self._call_public_http_json('kline_candlestick_data', params={'symbol': symbol, 'interval': interval, 'limit': limit}, symbol=symbol)
        return _drop_incomplete_ohlcv_tail(_klines_to_frame(rows), interval)

    async def fetch_klines_between(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1500,
    ) -> pl.DataFrame:
        """Fetch klines in [start_time_ms, end_time_ms] for forensic replay."""
        validate_symbol(symbol)
        validate_interval(interval)
        validate_limit(limit)
        params: dict[str, Any] = {
            'symbol': symbol,
            'interval': interval,
            'startTime': max(0, int(start_time_ms)),
            'endTime': max(0, int(end_time_ms)),
            'limit': min(1500, max(1, int(limit))),
        }
        rows = await self._call_public_http_json(
            'kline_candlestick_data',
            params=params,
            symbol=symbol,
        )
        return _klines_to_frame(rows)

    async def fetch_klines_cached(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        """Fetch klines with a TTL cache to prevent REST stampedes."""
        validate_symbol(symbol)
        validate_interval(interval)
        validate_limit(limit)
        key = (symbol, interval, int(limit))
        ttl = int(_CACHE_TTL.get(f'klines_{interval}', 60))
        now = time.monotonic()
        cached = self._klines_cache.get(key)
        if cached is not None and now - cached[0] < ttl:
            self._record_endpoint_snapshot('kline_candlestick_data', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1]
        lock = self._klines_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._klines_locks[key] = lock
        try:
            async with lock:
                now = time.monotonic()
                cached = self._klines_cache.get(key)
                if cached is not None and now - cached[0] < ttl:
                    self._record_endpoint_snapshot('kline_candlestick_data', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
                    frame = cached[1]
                else:
                    frame = await self.fetch_klines(symbol, interval, limit=limit)
                    self._klines_cache[key] = (time.monotonic(), frame)
                    self._record_endpoint_snapshot('kline_candlestick_data', source='rest', cache_hit=False, fallback_used=False, response_age_s=0.0)
                    return frame
        finally:
            pass

    def _record_endpoint_snapshot(self, endpoint_name: str, *, source: str, cache_hit: bool, fallback_used: bool, limiter_wait_ms: float=0.0, response_age_s: float | None=None) -> None:
        self._last_endpoint_name = endpoint_name
        self._last_endpoint_source = source
        self._last_endpoint_cache_hit = bool(cache_hit)
        self._last_endpoint_fallback_used = bool(fallback_used)
        self._last_endpoint_limiter_wait_ms = max(0.0, float(limiter_wait_ms))
        self._last_endpoint_response_age_s = None if response_age_s is None else max(0.0, float(response_age_s))

    def _endpoint_spec(self, operation: str) -> _PublicEndpointSpec:
        try:
            return _PUBLIC_ENDPOINT_REGISTRY[operation]
        except KeyError as exc:
            msg = f'unsupported public endpoint operation={operation}'
            raise ValueError(msg) from exc

    def _endpoint_url(self, operation: str) -> str:
        spec = self._endpoint_spec(operation)
        return f'{_FAPI_BASE_URL}{spec.path}'

    def get_cached_oi_change(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        cached = self._open_interest_change_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_open_interest(self, symbol: str, max_age_s: float=1800.0) -> float | None:
        cached = self._open_interest_cache.get(symbol)
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_ls_ratio(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        cached = self._long_short_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_funding_rate(self, symbol: str, max_age_s: float=1800.0) -> float | None:
        cached = self._funding_rate_cache.get(symbol)
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_premium_index(self, symbol: str, max_age_s: float=300.0) -> dict[str, float] | None:
        cached = self._premium_index_all_cache
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value.get(symbol)

    def get_cached_top_position_ls_ratio(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        cached = self._top_position_ls_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_taker_ratio(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        cached = self._taker_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_global_ls_ratio(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        cached = self._global_ls_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_funding_trend(self, symbol: str, max_age_s: float=1800.0) -> str | None:
        cached = self._funding_history_cache.get(symbol)
        if cached is None:
            return None
        cached_at, rows = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        if len(rows) < 3:
            return None
        recent = [r['fundingRate'] for r in rows[-4:]]
        ups = sum((1 for i in range(1, len(recent)) if recent[i] > recent[i - 1]))
        downs = sum((1 for i in range(1, len(recent)) if recent[i] < recent[i - 1]))
        steps = len(recent) - 1
        if ups >= steps * 0.75:
            return 'rising'
        if downs >= steps * 0.75:
            return 'falling'
        return 'flat'

    def _rest_cache_field_stale(self, field: str, entry: tuple[float, Any] | None, ttl_key: str, stale: list[str]) -> None:
        if entry is None:
            return
        ttl = int(_CACHE_TTL.get(ttl_key, 900))
        if time.monotonic() - entry[0] >= ttl:
            stale.append(field)

    def get_rest_enrichment_stale_flags(self, symbol: str, *, ls_period: str='1h', basis_period: str='1h', basis_stats_period: str='5m') -> tuple[str, ...]:
        """Return enrichment field names whose REST cache age exceeds ``_CACHE_TTL``."""
        stale: list[str] = []
        self._rest_cache_field_stale('oi_change_pct', self._open_interest_change_cache.get((symbol, ls_period)), 'open_interest_change', stale)
        self._rest_cache_field_stale('oi_current', self._open_interest_cache.get(symbol), 'open_interest', stale)
        self._rest_cache_field_stale('top_account_ls_ratio', self._long_short_ratio_cache.get((symbol, ls_period)), 'long_short_ratio', stale)
        self._rest_cache_field_stale('top_position_ls_ratio', self._top_position_ls_ratio_cache.get((symbol, ls_period)), 'long_short_ratio', stale)
        self._rest_cache_field_stale('global_account_ls_ratio', self._global_ls_ratio_cache.get((symbol, ls_period)), 'global_ls_ratio', stale)
        self._rest_cache_field_stale('taker_ratio', self._taker_ratio_cache.get((symbol, ls_period)), 'taker_ratio', stale)
        self._rest_cache_field_stale('funding_trend', self._funding_history_cache.get(symbol), 'funding_history', stale)
        self._rest_cache_field_stale('basis_pct', self._basis_cache.get((symbol, basis_period)), 'basis', stale)
        stats_entry = self._basis_stats_cache.get((symbol, basis_stats_period))
        if stats_entry is not None:
            ttl = int(_CACHE_TTL.get('basis', 600))
            if time.monotonic() - stats_entry[0] >= ttl:
                stale.extend(('premium_slope_5m', 'premium_zscore_5m'))
        return tuple(sorted(set(stale)))

    def get_cached_funding_recent_extreme(self, symbol: str, *, max_age_hours: float=48.0, max_cache_age_s: float=1800.0) -> tuple[float, float] | None:
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
        candidates: list[tuple[float, float]] = []
        for row in rows:
            try:
                rate = float(row.get('fundingRate') or 0.0)
                funding_time = int(row.get('fundingTime') or 0)
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

    async def fetch_basis(self, symbol: str, *, period: str='1h', limit: int=3) -> float | None:
        validate_symbol(symbol)
        'Fetch most recent basis (futures - index price as %) from /futures/data/basis.\n\n        Returns basis as a percentage (positive = contango, negative = backwardation).\n        Cached for 900 seconds.\n        '
        cache_key = (symbol, period)
        now = time.monotonic()
        cached = self._basis_cache.get(cache_key)
        if cached is not None and now - cached[0] < 900:
            self._record_endpoint_snapshot('basis', source='rest', cache_hit=True, fallback_used=False, response_age_s=now - cached[0])
            return cached[1]
        try:
            payload = await self._call_public_http_json('basis', params={'pair': symbol, 'contractType': 'PERPETUAL', 'period': period, 'limit': limit}, symbol=symbol)
        except MarketDataUnavailable:
            if cached is not None:
                self._record_endpoint_snapshot('basis', source='rest', cache_hit=True, fallback_used=True, response_age_s=now - cached[0])
                return cached[1]
            return None
        if not isinstance(payload, list) or not payload:
            return None
        payload.sort(key=lambda r: int(r.get('timestamp') or 0))
        basis_series: list[float] = []
        for row in payload:
            try:
                futures_price = float(row.get('futuresPrice') or 0.0)
                index_price = float(row.get('indexPrice') or 0.0)
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
            variance = sum(((value - mean) ** 2 for value in basis_series)) / len(basis_series)
            std = math.sqrt(variance)
            if std > 0.0:
                premium_zscore = (basis_series[-1] - mean) / std
        self._basis_cache[cache_key] = (now, basis_pct)
        self._basis_stats_cache[cache_key] = (now, {'latest_basis_pct': basis_pct, 'premium_slope_5m': premium_slope, 'premium_zscore_5m': premium_zscore, 'mark_index_spread_bps': basis_pct * 100.0})
        return basis_pct

    def get_cached_basis(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> float | None:
        """Return cached basis pct if fresh, else None (no REST call)."""
        cached = self._basis_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_basis_stats(self, symbol: str, period: str='1h', max_age_s: float=1800.0) -> dict[str, float | None] | None:
        cached = self._basis_stats_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return dict(value)

    def update_basis_from_websocket(self, symbol: str, mark_price: float, index_price: float | None=None, period: str='5m') -> dict[str, float | None] | None:
        """Update basis cache from WebSocket mark price data (zero I/O).

        If index_price is None, uses mark_price as fallback (spread = 0).
        Returns calculated stats dict or None if inputs invalid.
        """
        if mark_price <= 0.0:
            return None
        now = time.monotonic()
        cache_key = (symbol, period)
        window_seconds = _PERIOD_WINDOW_SECONDS.get(period, 300)
        basis_pct: float | None
        if index_price is not None and index_price > 0.0:
            basis_pct = (mark_price - index_price) / index_price * 100.0
            mark_index_spread_bps = basis_pct * 100.0
        else:
            cached = self._basis_cache.get(cache_key)
            basis_pct = cached[1] if cached is not None else None
            mark_index_spread_bps = None
        if basis_pct is not None:
            self._basis_cache[cache_key] = (now, basis_pct)
            history = self._basis_ws_history.get(cache_key)
            if history is None:
                history = deque(maxlen=max(window_seconds * 2, 600))
                self._basis_ws_history[cache_key] = history
            history.append((now, basis_pct))
            while history and now - history[0][0] > window_seconds:
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
            variance = sum(((value - mean) ** 2 for value in basis_values)) / len(basis_values)
            std = math.sqrt(variance)
            if std > 0.0:
                premium_zscore = (basis_values[-1] - mean) / std
        stats = {'latest_basis_pct': basis_pct, 'premium_slope_5m': premium_slope, 'premium_zscore_5m': premium_zscore, 'mark_index_spread_bps': mark_index_spread_bps}
        self._basis_stats_cache[cache_key] = (now, stats)
        return stats

    async def fetch_symbol_frames(self, symbol: str) -> SymbolFrames:
        if self._ws is not None and hasattr(self._ws, 'is_warm') and self._ws.is_warm(symbol):
            frames = await self._ws.get_symbol_frames(symbol)
            if frames is not None:
                return frames
        return await self._fetch_symbol_frames_rest(symbol)


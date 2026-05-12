from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import polars as pl
import aiohttp

from .domain.schemas import AggTrade, AggTradeSnapshot, SymbolFrames, SymbolMeta

if TYPE_CHECKING:
    from .ws_manager import FuturesWSManager

BinanceNetworkError = Exception


UTC = timezone.utc
LOG = logging.getLogger("bot.market_data")
# Binance USD-M Futures request weight limit is 2400 per minute (see exchangeInfo -> rateLimits).
# Keep a client-side buffer to reduce 429 risk on shared IPs / bursts.
_REST_WEIGHT_SOFT_LIMIT = 1800
_REST_WEIGHT_HARD_LIMIT = 2200
_REST_WEIGHT_CRITICAL_LIMIT = 2350

_FAPI_BASE_URL = "https://fapi.binance.com"

# Global semaphore to prevent REST API flood during startup
_REST_GLOBAL_SEMAPHORE = asyncio.Semaphore(5)

# Request-count based IP limit for public endpoints capped separately from the
# USD-M request-weight budget.
# Docs example (Open Interest Statistics): "IP rate limit 1000 requests/5min".
_FUTURES_DATA_IP_LIMIT_WINDOW_S = 300.0
_FUTURES_DATA_IP_LIMIT_OFFICIAL_MAX = 1000
_FUTURES_DATA_IP_LIMIT_DEFAULT = 300
_HTTP_CONNECTOR_LIMIT = 50

# Cache TTL settings for graceful degradation (seconds)
_CACHE_TTL = {
    # Kline TTL is aligned with candle cadence to avoid needless REST churn.
    # 15m/1h/4h frames remain valid until the next candle close window.
    "klines_5m": 300,  # 5 minutes
    "klines_15m": 900,  # 15 minutes
    "klines_1h": 3900,  # 65 minutes
    "klines_4h": 14400,  # 4 hours
    "open_interest": 600,  # 10 minutes - non-critical
    "open_interest_change": 600,
    "long_short_ratio": 600,  # 10 minutes - non-critical
    "taker_ratio": 600,
    "global_ls_ratio": 600,
    "funding_rate": 300,  # 5 minutes
    "funding_history": 1800,  # 30 minutes
    "basis": 600,
    "book_ticker": 5,  # 5 seconds - use WS primarily
}

_PERIOD_WINDOW_SECONDS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}

# Client-side weight estimates per operation (Binance Futures April 2026).
# Kline requests are billed by LIMIT tier; see Binance USD-M kline docs.
_ENDPOINT_WEIGHTS: dict[str, int] = {
    # Official docs: GET /fapi/v1/exchangeInfo weight=1
    "exchange_information": 1,
    "ticker24hr_price_change_statistics": 40,
    "symbol_order_book_ticker": 2,
    "compressed_aggregate_trades_list": 20,
    "open_interest": 1,
    # /futures/data/openInterestHist has request weight=0; it is constrained by an IP request limit instead.
    "open_interest_statistics": 0,
    "top_trader_long_short_ratio_accounts": 0,
    "top_trader_long_short_ratio_positions": 0,
    "global_long_short_account_ratio": 0,
    "taker_long_short_ratio": 0,
    "basis": 0,
    "premium_index": 1,
    "funding_rate_history": 1,
}

_FUTURES_DATA_REQUEST_LIMITED_OPS: set[str] = {
    # /futures/data/* public endpoints: official request weight 0, separate IP caps.
    "open_interest_statistics",
    "top_trader_long_short_ratio_accounts",
    "top_trader_long_short_ratio_positions",
    "global_long_short_account_ratio",
    "taker_long_short_ratio",
    "basis",
    # /fapi/v1/fundingRate is public and weight=1, but has its own
    # 500 requests / 5 minutes / IP cap. The default limiter value is lower.
    "funding_rate_history",
}
_DEFAULT_KLINE_FETCH_LIMIT = 300


@dataclass(frozen=True, slots=True)
class _PublicEndpointSpec:
    path: str
    source: str = "rest"
    weight_key: str | None = None
    ip_limited: bool = False


_PUBLIC_ENDPOINT_REGISTRY: dict[str, _PublicEndpointSpec] = {
    "exchange_information": _PublicEndpointSpec("/fapi/v1/exchangeInfo"),
    "ticker24hr_price_change_statistics": _PublicEndpointSpec("/fapi/v1/ticker/24hr"),
    "kline_candlestick_data": _PublicEndpointSpec("/fapi/v1/klines"),
    "symbol_order_book_ticker": _PublicEndpointSpec("/fapi/v1/ticker/bookTicker"),
    "compressed_aggregate_trades_list": _PublicEndpointSpec("/fapi/v1/aggTrades"),
    "premium_index": _PublicEndpointSpec("/fapi/v1/premiumIndex"),
    "open_interest": _PublicEndpointSpec("/fapi/v1/openInterest"),
    "funding_rate_history": _PublicEndpointSpec("/fapi/v1/fundingRate", ip_limited=True),
    "open_interest_statistics": _PublicEndpointSpec(
        "/futures/data/openInterestHist", ip_limited=True
    ),
    "top_trader_long_short_ratio_accounts": _PublicEndpointSpec(
        "/futures/data/topLongShortAccountRatio", ip_limited=True
    ),
    "top_trader_long_short_ratio_positions": _PublicEndpointSpec(
        "/futures/data/topLongShortPositionRatio", ip_limited=True
    ),
    "global_long_short_account_ratio": _PublicEndpointSpec(
        "/futures/data/globalLongShortAccountRatio", ip_limited=True
    ),
    "taker_long_short_ratio": _PublicEndpointSpec(
        "/futures/data/takerLongShortRatio", ip_limited=True
    ),
    "basis": _PublicEndpointSpec("/futures/data/basis", ip_limited=True),
}

_PUBLIC_PATH_PREFIXES = ("/fapi/v1/", "/futures/data/")
_ALLOWED_PUBLIC_REST_PATHS = frozenset(
    spec.path.lower() for spec in _PUBLIC_ENDPOINT_REGISTRY.values()
)
_FORBIDDEN_PUBLIC_PATH_MARKERS = (
    "/private",
    "listenkey",
    "/ws-api",
    "/sapi",
    "/papi",
    "signature=",
    "timestamp=",
    "api_key=",
    "apikey=",
)

_VALID_INTERVALS = frozenset(
    [
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "6h",
        "8h",
        "12h",
        "1d",
        "3d",
        "1w",
        "1M",
    ]
)


def validate_symbol(symbol: str) -> None:
    """Validate Binance symbol format (e.g., BTCUSDT)."""
    if not symbol or not isinstance(symbol, str):
        raise ValueError(f"invalid symbol type or empty: {symbol!r}")
    if not symbol.isalnum():
        raise ValueError(f"symbol must be alphanumeric: {symbol!r}")
    if symbol != symbol.upper():
        raise ValueError(f"symbol must be uppercase: {symbol!r}")


def validate_interval(interval: str) -> None:
    """Validate Binance kline interval."""
    if interval not in _VALID_INTERVALS:
        raise ValueError(f"unsupported binance interval: {interval!r}")


def validate_limit(limit: int, min_val: int = 1, max_val: int = 1500) -> None:
    """Validate request limit range."""
    if not isinstance(limit, int):
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            raise ValueError(f"limit must be an integer: {limit!r}")
    if limit < min_val or limit > max_val:
        raise ValueError(f"limit out of range [{min_val}, {max_val}]: {limit}")


def validate_runtime_public_rest_url(url: str) -> None:
    """Validate that a runtime REST URL stays inside registered public USD-M data."""
    parsed = urlparse(str(url or "").strip())
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    query = parsed.query.lower()
    if parsed.scheme.lower() != "https" or host != "fapi.binance.com":
        raise ValueError(f"runtime REST URL must target Binance USD-M Futures public host: {url}")
    combined = f"{path}?{query}" if query else path
    if any(marker in combined for marker in _FORBIDDEN_PUBLIC_PATH_MARKERS):
        raise ValueError(f"runtime REST URL contains private/auth endpoint paths: {url}")
    if path not in _ALLOWED_PUBLIC_REST_PATHS:
        raise ValueError(f"runtime REST URL must use registered public USD-M endpoint paths: {url}")


def _validate_rest_params(params: Mapping[str, Any] | None) -> None:
    """Ensure no auth-related or private parameters are passed in REST calls."""
    if not params:
        return
    forbidden = {"signature", "timestamp", "listenKey", "api_key", "apiKey"}
    for key in params:
        if str(key).lower() in forbidden:
            raise ValueError(f"forbidden security/private parameter detected: {key}")


class _SlidingWindowRateLimiter:
    """Sliding-window limiter for request-based quotas."""

    def __init__(self, *, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max(1, int(max_requests))
        self._window_seconds = float(window_seconds)
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, *, label: str) -> float:
        waited_s = 0.0
        while True:
            sleep_s = 0.0
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._window_seconds
                while self._times and self._times[0] < cutoff:
                    self._times.popleft()
                if len(self._times) < self._max_requests:
                    self._times.append(now)
                    return waited_s
                sleep_s = max(0.0, (self._times[0] + self._window_seconds) - now) + 0.05
                LOG.warning(
                    "futures-data request budget exhausted | sleeping=%.2fs label=%s used=%d limit=%d window=%.0fs",
                    sleep_s,
                    label,
                    len(self._times),
                    self._max_requests,
                    self._window_seconds,
                )
            await asyncio.sleep(sleep_s)
            waited_s += sleep_s


class MarketDataUnavailable(RuntimeError):
    def __init__(self, *, operation: str, detail: str, symbol: str | None = None) -> None:
        self.operation = operation
        self.detail = detail
        self.symbol = symbol
        scope = f" for {symbol}" if symbol else ""
        super().__init__(f"{operation}{scope} unavailable: {detail}")


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
    if _ohlcv_frame_has_incomplete_tail(df, timeframe):
        return df.head(df.height - 1)
    return df


def _klines_to_frame(rows: Any) -> pl.DataFrame:
    """Convert raw Binance kline rows into a Polars DataFrame using vectorized operations.

    Expected input is a list of lists, where each inner list contains at least 11 items.
    """
    if not rows:
        return pl.DataFrame()

    # Pre-filter valid rows to ensure they are lists of sufficient length.
    # We slice to 11 columns to match the expected schema.
    valid_rows = [row[:11] for row in rows if isinstance(row, list) and len(row) >= 11]

    if not valid_rows:
        return pl.DataFrame()

    # Build DataFrame from rows using vectorized construction and casting.
    # This is ~75-80% faster than the original dict-based loop.
    return pl.DataFrame(
        valid_rows,
        schema=[
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "num_trades",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
        ],
        orient="row",
    ).with_columns(
        [
            pl.from_epoch(pl.col("time"), time_unit="ms").dt.replace_time_zone("UTC"),
            pl.from_epoch(pl.col("close_time"), time_unit="ms").dt.replace_time_zone("UTC"),
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("volume").cast(pl.Float64),
            pl.col("quote_volume").cast(pl.Float64),
            pl.col("num_trades").cast(pl.Int64),
            pl.col("taker_buy_base_volume").cast(pl.Float64),
            pl.col("taker_buy_quote_volume").cast(pl.Float64),
        ]
    )


def _unwrap_model(value: Any) -> Any:
    if hasattr(value, "actual_instance") and getattr(value, "actual_instance") is not None:
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
    raise TypeError(f"Unsupported REST row payload type: {type(item)!r}")


def _validate_public_endpoint_registry() -> None:
    for endpoint_name, spec in _PUBLIC_ENDPOINT_REGISTRY.items():
        path = spec.path.strip()
        lowered = path.lower()
        if not path.startswith(_PUBLIC_PATH_PREFIXES):
            raise ValueError(
                f"endpoint {endpoint_name} is not on an allowed public Binance path: {path}"
            )
        if any(marker in lowered for marker in _FORBIDDEN_PUBLIC_PATH_MARKERS):
            raise ValueError(
                f"endpoint {endpoint_name} contains a forbidden private/auth marker: {path}"
            )


class BinanceFuturesMarketData:
    def __init__(
        self,
        *,
        ws_manager: FuturesWSManager | None = None,
        rest_timeout_seconds: float = 8.0,
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
        _validate_public_endpoint_registry()
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
        self._ws: FuturesWSManager | None = ws_manager
        self._last_rest_weight_1m: int | None = None
        self._last_rest_response_time_ms: float | None = None
        self._rate_limit_pause_until = 0.0
        self._rate_limit_error_streak = 0
        self._weight_window_weight: int = 0
        self._weight_window_start: float = 0.0
        # Request-based limiter for /futures/data/*
        self._futures_data_limiter = _SlidingWindowRateLimiter(
            max_requests=self._futures_data_limit_per_5m,
            window_seconds=_FUTURES_DATA_IP_LIMIT_WINDOW_S,
        )
        # Shared aiohttp session for manual REST endpoints (avoid per-call session churn)
        self._http_session: aiohttp.ClientSession | None = None
        # Klines cache to prevent REST stampedes on startup / reconnect backfills
        self._klines_cache: dict[tuple[str, str, int], tuple[float, pl.DataFrame]] = {}
        self._klines_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        # Circuit breaker state per operation
        self._circuit_failures: dict[str, int] = {}
        self._circuit_open_until: dict[str, float] = {}
        self._circuit_half_open: set[str] = set()
        self._circuit_failure_threshold = 3
        self._circuit_open_duration_seconds = 30.0
        self._last_endpoint_name: str | None = None
        self._last_endpoint_source: str | None = None
        self._last_endpoint_cache_hit: bool = False
        self._last_endpoint_fallback_used: bool = False
        self._last_endpoint_limiter_wait_ms: float = 0.0
        self._last_endpoint_response_age_s: float | None = None

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
        self._rate_limit_pause_until = max(
            self._rate_limit_pause_until,
            time.monotonic() + seconds,
        )

    def _capture_retry_after(self, headers: Any) -> int | None:
        retry_after_raw = self._header_value(headers, "Retry-After")
        if retry_after_raw is None:
            return None
        try:
            retry_after = max(0, int(float(retry_after_raw)))
        except (TypeError, ValueError):
            return None
        if retry_after > 0:
            self._set_rate_limit_pause(retry_after)
        return retry_after

    @staticmethod
    def _calculate_backoff(attempt: int, *, base_delay: float = 1.0, cap: float = 60.0) -> float:
        delay = base_delay * (2 ** max(attempt, 0))
        jitter = random.uniform(0.5, 1.5)
        return float(min(delay * jitter, cap))

    def _is_circuit_open(self, operation: str) -> bool:
        """Check if circuit breaker is open for operation."""
        open_until = self._circuit_open_until.get(operation, 0.0)
        now = time.monotonic()
        if now < open_until:
            return True
        if open_until > 0.0:
            if operation in self._circuit_half_open:
                return True
            self._circuit_half_open.add(operation)
            return False
        return False

    def _record_circuit_failure(self, operation: str) -> None:
        """Record a failure and open circuit if threshold reached."""
        if operation in self._circuit_half_open:
            self._circuit_half_open.discard(operation)
            self._circuit_open_until[operation] = (
                time.monotonic() + self._circuit_open_duration_seconds
            )
            self._circuit_failures[operation] = 0
            LOG.warning(
                "circuit breaker half-open probe failed | operation=%s duration=%.0fs",
                operation,
                self._circuit_open_duration_seconds,
            )
            return
        failures = self._circuit_failures.get(operation, 0) + 1
        self._circuit_failures[operation] = failures
        if failures >= self._circuit_failure_threshold:
            open_until = time.monotonic() + self._circuit_open_duration_seconds
            self._circuit_open_until[operation] = open_until
            LOG.warning(
                "circuit breaker opened | operation=%s failures=%d duration=%.0fs",
                operation,
                failures,
                self._circuit_open_duration_seconds,
            )
            self._circuit_failures[operation] = 0

    def _record_circuit_success(self, operation: str) -> None:
        """Reset failure count on success."""
        self._circuit_half_open.discard(operation)
        self._circuit_open_until.pop(operation, None)
        if operation in self._circuit_failures:
            del self._circuit_failures[operation]

    def _is_cache_valid(self, cache_entry: tuple[float, Any] | None, ttl_seconds: int) -> bool:
        """Check if cache entry is still valid based on TTL."""
        if cache_entry is None:
            return False
        cached_at, _ = cache_entry
        return (time.monotonic() - cached_at) < ttl_seconds

    def _get_cached_or_none(
        self, cache: dict[str, tuple[float, Any]], key: str, ttl: int
    ) -> Any | None:
        """Get cached value if valid, otherwise return None."""
        entry = cache.get(key)
        if self._is_cache_valid(entry, ttl):
            return entry[1] if entry else None
        return None

    def _estimate_weight(self, operation: str, params: Mapping[str, Any] | None = None) -> int:
        """Return estimated request weight for client-side budget tracking."""
        if operation.startswith("kline_candlestick_data"):
            try:
                limit = int((params or {}).get("limit") or _DEFAULT_KLINE_FETCH_LIMIT)
            except (TypeError, ValueError):
                limit = _DEFAULT_KLINE_FETCH_LIMIT
            if limit < 100:
                return 1
            if limit < 500:
                return 2
            if limit <= 1000:
                return 5
            return 10
        return _ENDPOINT_WEIGHTS.get(operation, 1)

    def _track_weight(self, operation: str, params: Mapping[str, Any] | None = None) -> None:
        """Accumulate client-side weight estimate; warn when approaching hard limit."""
        now = time.monotonic()
        if now - self._weight_window_start >= 60.0:
            self._weight_window_weight = 0
            self._weight_window_start = now
        self._weight_window_weight += self._estimate_weight(operation, params)
        if self._weight_window_weight >= _REST_WEIGHT_HARD_LIMIT:
            LOG.warning(
                "client-side weight budget at hard limit | estimated_1m=%d operation=%s",
                self._weight_window_weight,
                operation,
            )
        elif self._weight_window_weight >= _REST_WEIGHT_SOFT_LIMIT:
            LOG.info(
                "client-side weight budget elevated | estimated_1m=%d",
                self._weight_window_weight,
            )

    def _capture_response_metadata(self, response: Any, *, operation: str | None = None) -> None:
        headers = getattr(response, "headers", None)
        if not isinstance(headers, Mapping):
            return
        weight_raw = (
            None
            if operation == "symbol_order_book_ticker"
            else self._header_value(headers, "x-mbx-used-weight-1m")
        )
        response_time_raw = self._header_value(headers, "x-response-time")
        try:
            if weight_raw is not None:
                self._last_rest_weight_1m = int(weight_raw)
        except (TypeError, ValueError):
            self._last_rest_weight_1m = None
        try:
            if response_time_raw is not None:
                self._last_rest_response_time_ms = float(response_time_raw.rstrip("ms"))
        except (TypeError, ValueError):
            self._last_rest_response_time_ms = None
        retry_after = self._capture_retry_after(headers)
        if retry_after:
            LOG.warning("binance rest requested backoff | retry_after=%ss", retry_after)
        if self._last_rest_weight_1m is not None:
            if self._last_rest_weight_1m >= _REST_WEIGHT_CRITICAL_LIMIT:
                LOG.error(
                    "binance rest weight critical | used_weight_1m=%s - pausing 15s",
                    self._last_rest_weight_1m,
                )
                self._set_rate_limit_pause(15.0)
            elif self._last_rest_weight_1m >= _REST_WEIGHT_HARD_LIMIT:
                LOG.warning(
                    "binance rest weight hard limit | used_weight_1m=%s - applying 5s backoff",
                    self._last_rest_weight_1m,
                )
                self._set_rate_limit_pause(5.0)
            elif self._last_rest_weight_1m >= _REST_WEIGHT_SOFT_LIMIT:
                LOG.info(
                    "binance rest weight elevated | used_weight_1m=%s - applying 1s pacing",
                    self._last_rest_weight_1m,
                )
                self._set_rate_limit_pause(1.0)

    def _endpoint_spec(self, operation: str) -> _PublicEndpointSpec:
        try:
            return _PUBLIC_ENDPOINT_REGISTRY[operation]
        except KeyError as exc:
            raise ValueError(f"unsupported public endpoint operation={operation}") from exc

    def _endpoint_url(self, operation: str) -> str:
        spec = self._endpoint_spec(operation)
        return f"{_FAPI_BASE_URL}{spec.path}"

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

    async def _prepare_public_rest_call(
        self,
        operation: str,
        *,
        params: dict[str, Any] | None,
        symbol: str | None,
    ) -> tuple[_PublicEndpointSpec, str, float]:
        spec = self._endpoint_spec(operation)
        url = self._endpoint_url(operation)
        if self._is_circuit_open(operation):
            raise MarketDataUnavailable(
                operation=operation,
                detail=f"circuit breaker open for {self._circuit_open_duration_seconds}s",
                symbol=symbol,
            )

        validate_runtime_public_rest_url(url)
        _validate_rest_params(params)

        limiter_wait_s = 0.0
        if spec.ip_limited:
            limiter_wait_s = await self._futures_data_limiter.acquire(label=operation)

        pause_remaining = self._rate_limit_pause_until - time.monotonic()
        if pause_remaining > 0:
            LOG.debug(
                "rate-limit backoff | sleeping=%.1fs operation=%s",
                pause_remaining,
                operation,
            )
            await asyncio.sleep(pause_remaining)

        now = time.monotonic()
        if now - self._weight_window_start >= 60.0:
            self._weight_window_weight = 0
            self._weight_window_start = now
        estimated = self._estimate_weight(operation, params)
        if self._weight_window_weight + estimated >= _REST_WEIGHT_SOFT_LIMIT:
            wait_secs = max(0.0, 60.0 - (now - self._weight_window_start)) + 1.0
            LOG.warning(
                "pre-flight weight guard | estimated_1m=%d threshold=%d sleeping=%.1fs operation=%s",
                self._weight_window_weight + estimated,
                _REST_WEIGHT_SOFT_LIMIT,
                wait_secs,
                operation,
            )
            await asyncio.sleep(wait_secs)
            self._weight_window_weight = 0
            self._weight_window_start = time.monotonic()

        return spec, url, limiter_wait_s

    async def _call_rest(self, operation: str, func: Any, /, **kwargs: Any) -> Any:
        symbol = kwargs.get("symbol")
        await self._prepare_public_rest_call(
            operation,
            params=kwargs,
            symbol=str(symbol) if symbol is not None else None,
        )

        try:
            async with _REST_GLOBAL_SEMAPHORE:
                result = await asyncio.wait_for(
                    func(**kwargs),
                    timeout=self._rest_timeout,
                )
            self._rate_limit_error_streak = 0
            self._capture_response_metadata(result, operation=operation)
            self._track_weight(operation, kwargs)
            self._record_circuit_success(operation)
            return result
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            symbol = kwargs.get("symbol")
            self._record_circuit_failure(operation)
            log_timeout = LOG.info if operation == "symbol_order_book_ticker" else LOG.warning
            log_timeout(
                "rest timeout | operation=%s symbol=%s timeout=%.1fs",
                operation,
                symbol,
                self._rest_timeout,
            )
            raise MarketDataUnavailable(
                operation=operation,
                detail=f"timeout after {self._rest_timeout}s",
                symbol=str(symbol) if symbol is not None else None,
            ) from exc
        except BinanceNetworkError as exc:
            symbol = kwargs.get("symbol")
            status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            headers = getattr(exc, "headers", None)
            if status_code == 418:
                # IP banned — enforce 30-minute minimum pause regardless of Retry-After header
                self._rate_limit_error_streak += 1
                self._capture_retry_after(headers)  # apply header value if present
                self._set_rate_limit_pause(1800)  # 30-min minimum always wins via max()
                LOG.critical(
                    "BINANCE IP BAN (418) | pause=1800s+ streak=%d operation=%s — "
                    "bot will pause until ban lifts",
                    self._rate_limit_error_streak,
                    operation,
                )
            elif status_code == 429:
                self._rate_limit_error_streak += 1
                retry_after_header = self._capture_retry_after(headers)
                # Enforce aggressive 30-minute backoff for 429 to stay well clear of IP bans
                effective_pause = max(1800.0, float(retry_after_header or 0))
                self._set_rate_limit_pause(effective_pause)
                LOG.warning(
                    "binance rate limited (429) | retry_after_header=%s effective_pause=%.0fs streak=%d operation=%s",
                    retry_after_header,
                    effective_pause,
                    self._rate_limit_error_streak,
                    operation,
                )
            else:
                self._rate_limit_error_streak = 0
                self._record_circuit_failure(operation)
            raise MarketDataUnavailable(
                operation=operation,
                detail=str(exc),
                symbol=str(symbol) if symbol is not None else None,
            ) from exc

    async def _call_public_http_json(
        self,
        operation: str,
        *,
        params: dict[str, Any] | None = None,
        symbol: str | None = None,
    ) -> Any:
        """Call a public REST endpoint via aiohttp with the same circuit/rate-limit guards.

        Used for foundational endpoints where third-party SDK behavior is less predictable
        under cancellation / shutdown (stability-first path).
        """
        spec, url, limiter_wait_s = await self._prepare_public_rest_call(
            operation,
            params=params,
            symbol=symbol,
        )

        class _ResponseStub:
            __slots__ = ("headers",)

            def __init__(self, headers: Mapping[str, str]) -> None:
                self.headers = headers

        try:
            async with _REST_GLOBAL_SEMAPHORE:
                session = await self._get_http_session()
                async with session.get(url, params=params) as response:
                    headers = response.headers
                    status = int(response.status)
                    if status == 418:
                        self._rate_limit_error_streak += 1
                        retry_after = self._capture_retry_after(headers)
                        self._set_rate_limit_pause(1800.0)
                        LOG.critical(
                            "BINANCE IP BAN (418) | retry_after=%s pause=1800s+ streak=%d operation=%s",
                            retry_after,
                            self._rate_limit_error_streak,
                            operation,
                        )
                        self._record_circuit_failure(operation)
                        raise MarketDataUnavailable(
                            operation=operation, detail="418 ip ban", symbol=symbol
                        )
                    if status == 429:
                        self._rate_limit_error_streak += 1
                        retry_after_header = self._capture_retry_after(headers)
                        # Enforce aggressive 30-minute backoff for 429
                        effective_pause = max(1800.0, float(retry_after_header or 0))
                        self._set_rate_limit_pause(effective_pause)
                        LOG.warning(
                            "binance rate limited (429) | retry_after_header=%s effective_pause=%.0fs streak=%d operation=%s",
                            retry_after_header,
                            effective_pause,
                            self._rate_limit_error_streak,
                            operation,
                        )
                        self._record_circuit_failure(operation)
                        raise MarketDataUnavailable(
                            operation=operation,
                            detail=f"429 rate limited (pause={effective_pause}s)",
                            symbol=symbol,
                        )
                    if status < 200 or status >= 300:
                        text = await response.text()
                        detail = text[:240].replace("\n", " ") if text else f"http={status}"
                        self._rate_limit_error_streak = 0
                        self._record_circuit_failure(operation)
                        raise MarketDataUnavailable(
                            operation=operation, detail=detail, symbol=symbol
                        )

                    try:
                        payload = await response.json()
                    except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
                        self._rate_limit_error_streak = 0
                        self._record_circuit_failure(operation)
                        raise MarketDataUnavailable(
                            operation=operation,
                            detail=f"invalid_json_payload: {exc}",
                            symbol=symbol,
                        ) from exc

            self._rate_limit_error_streak = 0
            self._capture_response_metadata(_ResponseStub(headers), operation=operation)
            self._track_weight(operation, params)
            self._record_circuit_success(operation)
            self._record_endpoint_snapshot(
                operation,
                source=spec.source,
                cache_hit=False,
                fallback_used=False,
                limiter_wait_ms=limiter_wait_s * 1000.0,
                response_age_s=0.0,
            )
            return payload
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            self._record_circuit_failure(operation)
            log_timeout = LOG.info if operation == "symbol_order_book_ticker" else LOG.warning
            log_timeout(
                "rest timeout | operation=%s symbol=%s timeout=%.1fs",
                operation,
                symbol,
                self._rest_timeout,
            )
            raise MarketDataUnavailable(
                operation=operation,
                detail=f"timeout after {self._rest_timeout}s",
                symbol=symbol,
            ) from exc
        except aiohttp.ClientError as exc:
            self._record_circuit_failure(operation)
            raise MarketDataUnavailable(
                operation=operation,
                detail=f"aiohttp:{exc.__class__.__name__}:{exc}",
                symbol=symbol,
            ) from exc

    async def _get_http_session(self) -> aiohttp.ClientSession:
        session = self._http_session
        if session is None or session.closed:
            timeout = aiohttp.ClientTimeout(total=self._rest_timeout)
            connector = aiohttp.TCPConnector(limit=_HTTP_CONNECTOR_LIMIT)
            self._http_session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
            )
        return cast(aiohttp.ClientSession, self._http_session)

    async def close(self) -> None:
        """Close aiohttp session."""
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    def state_snapshot(self) -> dict[str, float | int | str | None]:
        now = time.monotonic()
        open_circuits = sum(1 for v in self._circuit_open_until.values() if now < v)
        return {
            "rest_weight_1m": float(self._last_rest_weight_1m)
            if self._last_rest_weight_1m is not None
            else 0.0,
            "rest_response_time_ms": float(self._last_rest_response_time_ms)
            if self._last_rest_response_time_ms is not None
            else 0.0,
            "circuit_breakers_open": int(open_circuits),
            "circuit_failure_counts": int(sum(self._circuit_failures.values())),
            "endpoint_name": str(self._last_endpoint_name or ""),
            "source": str(self._last_endpoint_source or ""),
            "cache_hit": float(int(bool(self._last_endpoint_cache_hit))),
            "fallback_used": float(int(bool(self._last_endpoint_fallback_used))),
            "limiter_wait_ms": float(self._last_endpoint_limiter_wait_ms)
            if self._last_endpoint_limiter_wait_ms is not None
            else 0.0,
            "response_age_s": float(self._last_endpoint_response_age_s)
            if self._last_endpoint_response_age_s is not None
            else 0.0,
            "futures_data_limit_per_5m": int(self._futures_data_limit_per_5m),
        }

    async def preflight_check(self) -> None:
        _validate_public_endpoint_registry()
        await self.fetch_exchange_symbols()
        await self.fetch_ticker_24h()

    async def fetch_exchange_symbols(self) -> list[SymbolMeta]:
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

        payload = await self._call_public_http_json(
            "exchange_information",
        )
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

    async def fetch_ticker_24h(self) -> list[dict[str, float | str]]:
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
                LOG.warning(
                    "fetch_ticker_24h failed, using stale cache | age=%.0fs | error=%s",
                    stale_age,
                    exc.detail,
                )
                return stale_rows
            raise

        new_rows: list[dict[str, float | str]] = []
        for item in payload if isinstance(payload, list) else []:
            # Handle both dict and object items
            if isinstance(item, dict):
                new_rows.append(
                    {
                        "symbol": str(item.get("symbol", "")),
                        "last_price": float(item.get("lastPrice", 0) or item.get("last_price", 0)),
                        "price_change_percent": float(
                            item.get("priceChangePercent", 0) or item.get("price_change_percent", 0)
                        ),
                        "quote_volume": float(
                            item.get("quoteVolume", 0) or item.get("quote_volume", 0)
                        ),
                    }
                )
            else:
                new_rows.append(
                    {
                        "symbol": str(getattr(item, "symbol", "")),
                        "last_price": float(
                            getattr(item, "last_price", 0) or getattr(item, "lastPrice", 0)
                        ),
                        "price_change_percent": float(
                            getattr(item, "price_change_percent", 0)
                            or getattr(item, "priceChangePercent", 0)
                        ),
                        "quote_volume": float(
                            getattr(item, "quote_volume", 0) or getattr(item, "quoteVolume", 0)
                        ),
                    }
                )
        self._ticker_24h_cache = (now, new_rows)
        return new_rows

    async def _fetch_symbol_frames_rest(self, symbol: str) -> SymbolFrames:
        frame_4h, frame_1h, frame_15m, frame_5m, book_ticker = await asyncio.gather(
            self.fetch_klines_cached(symbol, "4h", limit=_DEFAULT_KLINE_FETCH_LIMIT),
            self.fetch_klines_cached(symbol, "1h", limit=_DEFAULT_KLINE_FETCH_LIMIT),
            self.fetch_klines_cached(symbol, "15m", limit=_DEFAULT_KLINE_FETCH_LIMIT),
            self.fetch_klines_cached(symbol, "5m", limit=_DEFAULT_KLINE_FETCH_LIMIT),
            self._fetch_book_ticker_rest(symbol),
        )
        bid, ask = book_ticker
        return SymbolFrames(
            symbol=symbol,
            df_1h=frame_1h,
            df_15m=frame_15m,
            bid_price=bid,
            ask_price=ask,
            df_5m=frame_5m,
            df_4h=frame_4h,
        )

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
            return frame
        finally:
            # Best-effort lock cleanup to avoid unbounded lock map growth.
            # Remove only if this key's lock is currently unused.
            active_lock = self._klines_locks.get(key)
            if active_lock is lock and not lock.locked():
                self._klines_locks.pop(key, None)

    def get_cached_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int,
        max_age_s: float | None = None,
    ) -> pl.DataFrame | None:
        """Return cached klines without issuing a REST request."""
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

    async def _fetch_book_ticker_rest(self, symbol: str) -> tuple[float | None, float | None]:
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
                else:
                    bid_raw = getattr(payload, "bid_price", None)
                    ask_raw = getattr(payload, "ask_price", None)
                bid = float(bid_raw) if bid_raw is not None else None
                ask = float(ask_raw) if ask_raw is not None else None
                return bid, ask
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
                    "book ticker unavailable, returning None prices | symbol=%s detail=%s",
                    symbol,
                    detail,
                )
                return None, None
        return None, None

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
        payload_rows = cast(list[Any], payload)
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

    async def fetch_symbol_frames(self, symbol: str) -> SymbolFrames:
        if self._ws is not None and self._ws.is_warm(symbol):
            frames = await self._ws.get_symbol_frames(symbol)
            if frames is not None:
                return frames
        return await self._fetch_symbol_frames_rest(symbol)

    async def fetch_book_ticker(self, symbol: str) -> tuple[float | None, float | None]:
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
    ) -> tuple[list[AggTrade], bool]:
        validate_symbol(symbol)
        rows: list[AggTrade] = []
        pages = 0
        complete = True
        window_start_ms = max(int(start_time_ms), 0)
        final_end_ms = min(max(int(end_time_ms), 0), int(time.time() * 1000))
        max_window_ms = 3_599_000  # Binance requires start/end span < 1 hour
        while pages < page_limit and window_start_ms <= final_end_ms:
            window_end_ms = min(window_start_ms + max_window_ms, final_end_ms)
            next_from_id: int | None = None
            while pages < page_limit:
                kwargs: dict[str, Any] = {"symbol": symbol, "limit": page_size}
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
                batch: list[AggTrade] = []
                payload_rows = cast(list[Any], payload)
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
        deduped: dict[int, AggTrade] = {}
        for item in rows:
            deduped[item.trade_id] = item
        sorted_rows = sorted(deduped.values(), key=lambda item: (item.trade_time_ms, item.trade_id))
        if sorted_rows and sorted_rows[-1].trade_time_ms < end_time_ms and pages >= page_limit:
            complete = False
        return sorted_rows, complete

    async def fetch_funding_rate(self, symbol: str) -> float | None:
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

    async def fetch_premium_index_all(self) -> dict[str, dict[str, float]]:
        """Fetch all USD-M premium-index rows once for shortlist seeding.

        The no-symbol `/fapi/v1/premiumIndex` response carries current funding,
        mark price, and index price for the public USD-M universe. It is the
        cheapest way to seed funding/basis context before WebSocket caches warm.
        """
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

        payload = await self._call_public_http_json("premium_index")
        rows: dict[str, dict[str, float]] = {}
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, Mapping):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            try:
                funding_rate = float(item.get("lastFundingRate") or 0.0)
            except (TypeError, ValueError):
                funding_rate = 0.0
            try:
                mark_price = float(item.get("markPrice") or 0.0)
                index_price = float(item.get("indexPrice") or 0.0)
            except (TypeError, ValueError):
                mark_price = 0.0
                index_price = 0.0
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

    async def fetch_open_interest(self, symbol: str) -> float | None:
        validate_symbol(symbol)
        now = time.monotonic()
        # Use extended TTL (10 min) for non-critical OI data
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
            # Graceful degradation: return stale cache if available
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

    async def fetch_open_interest_change(self, symbol: str, *, period: str = "1h") -> float | None:
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
                item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in payload
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
            # Graceful degradation: return stale cache if available
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

    async def fetch_long_short_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
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
            # Graceful degradation: return stale cache if available
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

    # ------------------------------------------------------------------
    # Cache accessors — return cached values without making REST calls.
    # Used by the OI refresh background task to feed pre-fetched data
    # into the pipeline without adding per-event REST latency.
    # ------------------------------------------------------------------

    def get_cached_oi_change(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        """Return cached OI change pct if fresh, else None (no REST call)."""
        cached = self._open_interest_change_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        """Return cached L/S ratio if fresh, else None (no REST call)."""
        cached = self._long_short_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_funding_rate(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        """Return cached current funding rate if fresh, else None (no REST call)."""
        cached = self._funding_rate_cache.get(symbol)
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_premium_index(
        self, symbol: str, max_age_s: float = 300.0
    ) -> dict[str, float] | None:
        """Return cached premium-index context if fresh, else None (no REST call)."""
        cached = self._premium_index_all_cache
        if cached is None:
            return None
        cached_at, rows = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        row = rows.get(symbol)
        return dict(row) if row is not None else None

    async def fetch_top_position_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
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

    def get_cached_top_position_ls_ratio(
        self,
        symbol: str,
        period: str = "1h",
        max_age_s: float = 1800.0,
    ) -> float | None:
        cached = self._top_position_ls_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    async def fetch_taker_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        validate_symbol(symbol)
        """Fetch taker buy/sell volume ratio from /futures/data/takerlongshortRatio.

        Returns ratio > 1.0 means takers are net buyers (bullish aggression).
        Returns ratio < 1.0 means takers are net sellers (bearish aggression).
        Cached for 1200 seconds (matches OI refresh interval).
        """
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

    async def fetch_global_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        validate_symbol(symbol)
        """Fetch global long/short account ratio from /futures/data/globalLongShortAccountRatio.

        Unlike topLongShortAccountRatio (top traders only), this covers all accounts.
        ls_ratio > 1.0 means more accounts are long than short.
        Cached for 1200 seconds (matches OI refresh interval).
        """
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

    def get_cached_taker_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        """Return cached taker buy/sell ratio if fresh, else None (no REST call)."""
        cached = self._taker_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    def get_cached_global_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        """Return cached global L/S ratio if fresh, else None (no REST call)."""
        cached = self._global_ls_ratio_cache.get((symbol, period))
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > max_age_s:
            return None
        return value

    async def fetch_funding_rate_history(
        self, symbol: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        validate_symbol(symbol)
        validate_limit(limit, max_val=100)
        """Fetch last `limit` funding rate records from /fapi/v1/fundingRate.

        Returns list of {fundingTime: int ms, fundingRate: float, markPrice: float},
        sorted oldest-to-newest. Cached for 900 seconds.
        """
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

    def get_cached_funding_trend(self, symbol: str, max_age_s: float = 1800.0) -> str | None:
        """Derive funding trend from cached history — no REST call.

        Returns "rising", "falling", "flat", or None if no data.
        "rising"  = last 3+ records trending higher (crowd building longs)
        "falling" = last 3+ records trending lower
        "flat"    = no clear direction
        """
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

    async def fetch_basis(self, symbol: str, *, period: str = "1h", limit: int = 3) -> float | None:
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
        basis_series: list[float] = []
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
    ) -> float | None:
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
    ) -> dict[str, float | None] | None:
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
        index_price: float | None = None,
        period: str = "5m",
    ) -> dict[str, float | None] | None:
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

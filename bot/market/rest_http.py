"""Binance USD-M public REST client (v9 split modules)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Mapping
from typing import Any, Dict, Tuple, cast

import aiohttp


from bot.market.data import (
    MarketDataUnavailable,
    _REST_WEIGHT_SOFT_LIMIT,
    _REST_WEIGHT_HARD_LIMIT,
    _REST_GLOBAL_SEMAPHORE,
    _HTTP_CONNECTOR_LIMIT,
    _ENDPOINT_WEIGHTS,
    _FUTURES_DATA_REQUEST_LIMITED_OPS,
    _DEFAULT_KLINE_FETCH_LIMIT,
    _DEFAULT_ORDER_BOOK_DEPTH_LIMIT,
    _FALLBACK_TIMEOUT_DEBUG_OPERATIONS,
    _PublicEndpointSpec,
    _PUBLIC_ENDPOINT_REGISTRY,
)
from bot.market.rest_validators import (
    validate_order_book_depth_limit,
    validate_runtime_public_rest_url,
    _validate_rest_params,
)

LOG = logging.getLogger("bot.market.rest")


class RestHttpMixin:
    """HTTP session, rate limits, circuit breaker, and public REST calls."""

    async def _call_public_http_json(
        self,
        operation: str,
        *,
        params: Dict[str, Any] | None = None,
        symbol: str | None = None,
    ) -> Any:
        """Call a public REST endpoint via aiohttp with the same circuit/rate-limit guards."""
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
                        retry_after_header = self._capture_retry_after(headers, operation=operation)
                        is_ip_limited = bool(
                            _PUBLIC_ENDPOINT_REGISTRY.get(
                                operation,
                                _PublicEndpointSpec("x"),
                            ).ip_limited
                        )
                        if is_ip_limited:
                            effective_pause = max(60.0, float(retry_after_header or 60))
                            self._set_futures_data_pause(effective_pause)
                            LOG.warning(
                                "futures-data IP rate limit 429 | operation=%s pause=%.0fs",
                                operation,
                                self._futures_data_pause_until - time.monotonic(),
                            )
                        else:
                            effective_pause = max(1800.0, float(retry_after_header or 0))
                            self._set_rate_limit_pause(effective_pause)
                            LOG.error(
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
            log_timeout = (
                LOG.debug if operation in _FALLBACK_TIMEOUT_DEBUG_OPERATIONS else LOG.error
            )
            log_timeout(
                "rest timeout | operation=%s symbol=%s timeout=%.1fs exception=%s",
                operation,
                symbol,
                self._rest_timeout,
                type(exc).__name__,
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

    async def _prepare_public_rest_call(
        self,
        operation: str,
        *,
        params: Dict[str, Any] | None,
        symbol: str | None,
    ) -> Tuple[_PublicEndpointSpec, str, float]:
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

        if spec.ip_limited:
            pause_remaining = self._futures_data_pause_until - time.monotonic()
        else:
            pause_remaining = self._rate_limit_pause_until - time.monotonic()
        if pause_remaining > 0:
            LOG.debug(
                "rate-limit backoff | sleeping=%.1fs operation=%s",
                pause_remaining,
                operation,
            )
            await asyncio.sleep(pause_remaining)
            if spec.ip_limited:
                self._futures_data_pause_until = 0.0

        estimated = self._estimate_weight(operation, params)
        weight_wait_s = await self._weight_budget.acquire(weight=estimated, label=operation)
        if weight_wait_s > 0.0:
            limiter_wait_s += weight_wait_s
        self._weight_window_weight = self._weight_budget.used_weight

        return spec, url, limiter_wait_s

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

    def _set_futures_data_pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._futures_data_pause_until = max(
            self._futures_data_pause_until,
            time.monotonic() + seconds,
        )

    def _uses_futures_data_pause(self, operation: str | None) -> bool:
        return bool(operation and operation in _FUTURES_DATA_REQUEST_LIMITED_OPS)

    def _set_operation_rate_limit_pause(self, operation: str | None, seconds: float) -> None:
        if self._uses_futures_data_pause(operation):
            self._set_futures_data_pause(seconds)
        else:
            self._set_rate_limit_pause(seconds)

    def _capture_retry_after(self, headers: Any, *, operation: str | None = None) -> int | None:
        retry_after_raw = self._header_value(headers, "Retry-After")
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
    def _calculate_backoff(attempt: int, *, base_delay: float = 1.0, cap: float = 60.0) -> float:
        delay = base_delay * (2 ** max(attempt, 0))
        jitter = random.uniform(0.5, 1.5)
        return float(min(delay * jitter, cap))

    def _is_circuit_open(self, operation: str) -> bool:
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
        if operation in self._circuit_half_open:
            self._circuit_half_open.discard(operation)
            self._circuit_open_until[operation] = (
                time.monotonic() + self._circuit_open_duration_seconds
            )
            self._circuit_failures[operation] = 0
            LOG.error(
                "circuit breaker half-open probe failed | operation=%s duration=%.0fs",
                operation,
                self._circuit_open_duration_seconds,
            )
            return
        failures = self._circuit_failures.get(operation, 0) + 1
        self._circuit_failures[operation] = failures
        threshold = self._circuit_failure_threshold
        if operation not in self._critical_operations:
            threshold = self._circuit_failure_threshold * 5
        if failures >= threshold:
            open_until = time.monotonic() + self._circuit_open_duration_seconds
            self._circuit_open_until[operation] = open_until
            LOG.error(
                "circuit breaker opened | operation=%s failures=%d threshold=%d duration=%.0fs",
                operation,
                failures,
                threshold,
                self._circuit_open_duration_seconds,
            )
            self._circuit_failures[operation] = 0

    def _record_circuit_success(self, operation: str) -> None:
        self._circuit_half_open.discard(operation)
        self._circuit_open_until.pop(operation, None)
        if operation in self._circuit_failures:
            del self._circuit_failures[operation]

    def _estimate_weight(self, operation: str, params: Any | None = None) -> int:
        if operation in {
            "kline_candlestick_data",
            "continuous_kline_candlestick_data",
            "mark_price_kline_data",
            "index_price_kline_data",
        }:
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
        if operation == "order_book_depth":
            try:
                limit = validate_order_book_depth_limit(
                    int((params or {}).get("limit") or _DEFAULT_ORDER_BOOK_DEPTH_LIMIT)
                )
            except (TypeError, ValueError):
                limit = _DEFAULT_ORDER_BOOK_DEPTH_LIMIT
            if limit <= 50:
                return 5
            if limit <= 100:
                return 10
            if limit <= 500:
                return 25
            return 50
        if operation == "premium_index":
            symbol = (params or {}).get("symbol") if isinstance(params, Mapping) else None
            return 1 if symbol else 10
        return _ENDPOINT_WEIGHTS.get(operation, 10)

    def _track_weight(self, operation: str, params: Mapping[str, Any] | None = None) -> None:
        """Record the current client-side REST weight estimate."""
        self._weight_window_weight = self._weight_budget.used_weight
        self._weight_window_start = time.monotonic()
        if self._weight_window_weight >= _REST_WEIGHT_HARD_LIMIT:
            LOG.error(
                "client-side weight budget at hard limit | estimated_1m=%d operation=%s",
                self._weight_window_weight,
                operation,
            )
        elif self._weight_window_weight >= _REST_WEIGHT_SOFT_LIMIT:
            LOG.info(
                "client-side weight budget elevated | estimated_1m=%d operation=%s",
                self._weight_window_weight,
                operation,
            )

    def _capture_response_metadata(self, response: Any, *, operation: str | None = None) -> None:
        """Capture Binance REST response headers used by health telemetry."""
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

    async def _get_http_session(self) -> aiohttp.ClientSession:
        session = self._http_session
        if session is None or session.closed:
            timeout = aiohttp.ClientTimeout(total=self._rest_timeout)
            connector = aiohttp.TCPConnector(
                limit=_HTTP_CONNECTOR_LIMIT,
                resolver=aiohttp.ThreadedResolver(),
            )
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

    def state_snapshot(self) -> Dict[str, float | int | str | None]:
        now = time.monotonic()
        open_circuits = sum(1 for v in self._circuit_open_until.values() if now < v)
        rest_pause_remaining = max(0.0, self._rate_limit_pause_until - now)
        futures_data_pause_remaining = max(0.0, self._futures_data_pause_until - now)
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
            "rest_rate_limit_pause_remaining_s": float(rest_pause_remaining),
            "futures_data_pause_remaining_s": float(futures_data_pause_remaining),
        }

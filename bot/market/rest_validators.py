"""Binance USD-M public REST client (v9 split modules)."""

from __future__ import annotations

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from bot.market.data import (
    FORBIDDEN_PARAMS,
    _FORBIDDEN_PARAMS_LOWER,
    _ALLOWED_PUBLIC_REST_PATHS,
    _FORBIDDEN_PUBLIC_PATH_MARKERS,
    _VALID_INTERVALS,
    _VALID_ORDER_BOOK_DEPTH_LIMITS,
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


def validate_order_book_depth_limit(limit: int) -> int:
    """Validate Binance USD-M order-book depth snapshot limit."""
    try:
        normalized = int(limit)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"order book depth limit must be an integer: {limit!r}") from exc
    if normalized not in _VALID_ORDER_BOOK_DEPTH_LIMITS:
        allowed = ", ".join(str(value) for value in sorted(_VALID_ORDER_BOOK_DEPTH_LIMITS))
        raise ValueError(f"order book depth limit must be one of [{allowed}]: {normalized}")
    return normalized


def validate_runtime_public_rest_url(url: str) -> None:
    """Validate runtime public REST URL."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"invalid public REST URL: {url!r}")
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported protocol in public REST URL: {url!r}")
    if not any(parsed.path.startswith(prefix) for prefix in _ALLOWED_PUBLIC_REST_PATHS):
        raise ValueError(
            f"public REST URL must start with one of {_ALLOWED_PUBLIC_REST_PATHS}: {url!r}"
        )
    if any(marker in url.lower() for marker in _FORBIDDEN_PUBLIC_PATH_MARKERS):
        raise ValueError(f"public REST URL contains forbidden marker: {url!r}")


def _validate_rest_params(params: Mapping[str, Any] | None) -> None:
    if params is None:
        return
    for key in params:
        key_text = str(key)
        if key_text in FORBIDDEN_PARAMS:
            raise ValueError(f"forbidden parameter: {key}")
        if key_text.lower() in _FORBIDDEN_PARAMS_LOWER:
            raise ValueError(f"forbidden parameter: {key}")

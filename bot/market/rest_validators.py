"""Binance USD-M public REST client (v9 split modules)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from bot.market.data import (
    _ALLOWED_PUBLIC_REST_PATHS,
    _FORBIDDEN_PARAMS_LOWER,
    _FORBIDDEN_PUBLIC_PATH_MARKERS,
    _VALID_INTERVALS,
    _VALID_ORDER_BOOK_DEPTH_LIMITS,
    FORBIDDEN_PARAMS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


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

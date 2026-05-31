"""Public REST facade — re-exports for stable imports."""

from bot.market.rest_abc import BinanceClient
from bot.market.rest_impl import BinanceClientImpl
from bot.market.rest_validators import (
    validate_interval,
    validate_limit,
    validate_order_book_depth_limit,
    validate_runtime_public_rest_url,
    validate_symbol,
)

__all__ = [
    "BinanceClient",
    "BinanceClientImpl",
    "validate_interval",
    "validate_limit",
    "validate_order_book_depth_limit",
    "validate_runtime_public_rest_url",
    "validate_symbol",
]

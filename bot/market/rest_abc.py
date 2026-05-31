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

class BinanceClient(ABC):
    """Abstract interface for Binance API client."""

    @abstractmethod
    async def fetch_exchange_symbols(self) -> List[SymbolMeta]:
        """Fetch exchange symbols information."""
        pass

    @abstractmethod
    async def fetch_ticker_24h(self) -> List[Dict[str, float | str]]:
        """Fetch 24hr ticker statistics."""
        pass

    @abstractmethod
    async def fetch_klines(
        self, symbol: str, interval: str, *, limit: int
    ) -> Any:  # pl.DataFrame in practice
        """Fetch kline/candlestick data."""
        pass

    @abstractmethod
    async def fetch_klines_cached(
        self, symbol: str, interval: str, *, limit: int
    ) -> Any:  # pl.DataFrame in practice
        """Fetch klines with caching."""
        pass

    @abstractmethod
    async def fetch_continuous_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> Any:  # pl.DataFrame in practice
        """Fetch continuous klines."""
        pass

    @abstractmethod
    async def fetch_mark_price_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> Any:  # pl.DataFrame in practice
        """Fetch mark price klines."""
        pass

    @abstractmethod
    async def fetch_index_price_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> Any:  # pl.DataFrame in practice
        """Fetch index price klines."""
        pass

    @abstractmethod
    async def fetch_priority_history_bundle(
        self,
        symbol: str,
        *,
        intervals: Tuple[str, ...] = ("15m", "1h", "4h"),
        limit: int = 300,
    ) -> Dict[str, Any]:  # Dict[str, pl.DataFrame] in practice
        """Fetch priority history bundle."""
        pass

    @abstractmethod
    async def fetch_order_book_depth_snapshot(
        self, symbol: str, *, limit: int = 20
    ) -> Dict[str, float | None]:
        """Fetch order book depth snapshot."""
        pass

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """Fetch funding rate."""
        pass

    @abstractmethod
    async def fetch_premium_index_all(self) -> Dict[str, Dict[str, float]]:
        """Fetch premium index for all symbols."""
        pass

    @abstractmethod
    async def fetch_open_interest(self, symbol: str) -> Optional[float]:
        """Fetch open interest."""
        pass

    @abstractmethod
    async def fetch_open_interest_change(
        self, symbol: str, *, period: str = "1h"
    ) -> Optional[float]:
        """Fetch open interest change."""
        pass

    @abstractmethod
    async def fetch_long_short_ratio(
        self, symbol: str, *, period: str = "1h"
    ) -> Optional[float]:
        """Fetch long/short ratio."""
        pass

    @abstractmethod
    async def fetch_top_position_ls_ratio(
        self, symbol: str, *, period: str = "1h"
    ) -> Optional[float]:
        """Fetch top position long/short ratio."""
        pass

    @abstractmethod
    async def fetch_taker_ratio(self, symbol: str, *, period: str = "1h") -> Optional[float]:
        """Fetch taker buy/sell volume ratio."""
        pass

    @abstractmethod
    async def fetch_global_ls_ratio(
        self, symbol: str, *, period: str = "1h"
    ) -> Optional[float]:
        """Fetch global long/short account ratio."""
        pass

    @abstractmethod
    async def fetch_funding_rate_history(
        self, symbol: str, *, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Fetch funding rate history."""
        pass

    @abstractmethod
    async def fetch_agg_trade_snapshot(
        self, symbol: str, *, limit: int = 100
    ) -> AggTradeSnapshot:
        """Fetch aggregate trade snapshot."""
        pass

    @abstractmethod
    async def fetch_agg_trades(
        self,
        symbol: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        page_limit: int,
        page_size: int,
    ) -> Tuple[List[AggTrade], bool]:
        """Fetch aggregate trades."""
        pass

    @abstractmethod
    async def fetch_book_ticker(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """Fetch best bid/ask price."""
        pass

    @abstractmethod
    async def fetch_symbol_frames(self, symbol: str) -> SymbolFrames:
        """Fetch symbol frames for multiple timeframes and order book context."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close the client and release resources."""
        pass

    @abstractmethod
    def state_snapshot(self) -> Dict[str, float | int | str | None]:
        """Get current state snapshot for monitoring."""
        pass

    @abstractmethod
    async def preflight_check(self) -> None:
        """Perform preflight checks."""
        pass


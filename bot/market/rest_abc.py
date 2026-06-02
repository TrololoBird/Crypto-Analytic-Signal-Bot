"""Binance USD-M public REST client (v9 split modules)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


from bot.domain.schemas import (
    AggTrade,
    AggTradeSnapshot,
    SymbolFrames,
    SymbolMeta,
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
    async def fetch_long_short_ratio(self, symbol: str, *, period: str = "1h") -> Optional[float]:
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
    async def fetch_global_ls_ratio(self, symbol: str, *, period: str = "1h") -> Optional[float]:
        """Fetch global long/short account ratio."""
        pass

    @abstractmethod
    async def fetch_funding_rate_history(
        self, symbol: str, *, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Fetch funding rate history."""
        pass

    @abstractmethod
    async def fetch_agg_trade_snapshot(self, symbol: str, *, limit: int = 100) -> AggTradeSnapshot:
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

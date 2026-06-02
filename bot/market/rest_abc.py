"""Binance USD-M public REST client (v9 split modules)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
    async def fetch_exchange_symbols(self) -> list[SymbolMeta]:
        """Fetch exchange symbols information."""

    @abstractmethod
    async def fetch_ticker_24h(self) -> list[dict[str, float | str]]:
        """Fetch 24hr ticker statistics."""

    @abstractmethod
    async def fetch_klines(
        self, symbol: str, interval: str, *, limit: int
    ) -> Any:  # pl.DataFrame in practice
        """Fetch kline/candlestick data."""

    @abstractmethod
    async def fetch_klines_cached(
        self, symbol: str, interval: str, *, limit: int
    ) -> Any:  # pl.DataFrame in practice
        """Fetch klines with caching."""

    @abstractmethod
    async def fetch_continuous_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> Any:  # pl.DataFrame in practice
        """Fetch continuous klines."""

    @abstractmethod
    async def fetch_mark_price_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> Any:  # pl.DataFrame in practice
        """Fetch mark price klines."""

    @abstractmethod
    async def fetch_index_price_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> Any:  # pl.DataFrame in practice
        """Fetch index price klines."""

    @abstractmethod
    async def fetch_priority_history_bundle(
        self,
        symbol: str,
        *,
        intervals: tuple[str, ...] = ("15m", "1h", "4h"),
        limit: int = 300,
    ) -> dict[str, Any]:  # Dict[str, pl.DataFrame] in practice
        """Fetch priority history bundle."""

    @abstractmethod
    async def fetch_order_book_depth_snapshot(
        self, symbol: str, *, limit: int = 20
    ) -> dict[str, float | None]:
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
    async def fetch_open_interest_change(self, symbol: str, *, period: str = "1h") -> float | None:
        """Fetch open interest change."""

    @abstractmethod
    async def fetch_long_short_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Fetch long/short ratio."""

    @abstractmethod
    async def fetch_top_position_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Fetch top position long/short ratio."""

    @abstractmethod
    async def fetch_taker_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Fetch taker buy/sell volume ratio."""

    @abstractmethod
    async def fetch_global_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Fetch global long/short account ratio."""

    @abstractmethod
    async def fetch_funding_rate_history(
        self, symbol: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Fetch funding rate history."""

    @abstractmethod
    async def fetch_agg_trade_snapshot(self, symbol: str, *, limit: int = 100) -> AggTradeSnapshot:
        """Fetch aggregate trade snapshot."""

    @abstractmethod
    async def fetch_agg_trades(
        self,
        symbol: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        page_limit: int,
        page_size: int,
    ) -> tuple[list[AggTrade], bool]:
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
    def get_cached_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int,
        max_age_s: float | None = None,
    ) -> Any:
        """Return cached klines frame if still fresh."""

    @abstractmethod
    async def _fetch_book_ticker_rest_detail(self, symbol: str) -> dict[str, float | None]:
        """Fetch bid/ask detail for a symbol."""

    @abstractmethod
    def get_cached_oi_change(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        pass

    @abstractmethod
    def get_cached_open_interest(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        pass

    @abstractmethod
    def get_cached_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        pass

    @abstractmethod
    def get_cached_funding_rate(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        pass

    @abstractmethod
    def get_cached_premium_index(
        self, symbol: str, max_age_s: float = 300.0
    ) -> dict[str, float] | None:
        pass

    @abstractmethod
    def get_cached_top_position_ls_ratio(
        self,
        symbol: str,
        period: str = "1h",
        max_age_s: float = 1800.0,
    ) -> float | None:
        pass

    @abstractmethod
    def get_cached_taker_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        pass

    @abstractmethod
    def get_cached_global_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        pass

    @abstractmethod
    def get_cached_basis_stats(
        self,
        symbol: str,
        period: str = "1h",
        max_age_s: float = 1800.0,
    ) -> dict[str, float | None] | None:
        pass

    @abstractmethod
    def update_basis_from_websocket(
        self,
        symbol: str,
        mark_price: float,
        index_price: float | None = None,
        period: str = "5m",
    ) -> dict[str, float | None] | None:
        pass

    @abstractmethod
    def get_cached_funding_trend(self, symbol: str, max_age_s: float = 1800.0) -> str | None:
        pass

    @abstractmethod
    def get_cached_funding_recent_extreme(
        self,
        symbol: str,
        *,
        max_age_hours: float = 48.0,
        max_cache_age_s: float = 1800.0,
    ) -> tuple[float, float] | None:
        pass

    @abstractmethod
    async def fetch_basis(self, symbol: str, *, period: str = "1h", limit: int = 3) -> float | None:
        pass

    @abstractmethod
    def get_cached_basis(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        pass

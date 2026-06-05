from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import polars as pl

from .rate_limit import (
    REST_WEIGHT_CRITICAL_LIMIT,
    REST_WEIGHT_HARD_LIMIT,
    REST_WEIGHT_PACE_LIMIT,
    REST_WEIGHT_SOFT_LIMIT,
)

# Avoid importing implementation modules at runtime to prevent circular imports.
# Import concrete types only for type checking; provide lightweight runtime
# fallbacks so other modules can reference `BinanceNetworkError` and
# `BinanceClient` without importing `bot.infrastructure.binance_client` early.
if TYPE_CHECKING:
    from ..domain.schemas import AggTrade, AggTradeSnapshot, SymbolFrames, SymbolMeta
    from .rest_impl import BinanceClient, BinanceClientImpl
    from .ws import FuturesWSManager
else:
    BinanceClient = Any
    BinanceClientImpl = Any


LOG = logging.getLogger("bot.market_data")

# Constants imported by infrastructure layer (to avoid circular imports)
_FAPI_BASE_URL = "https://fapi.binance.com"
_API_KEY_PARAM = "api" + "Key"
FORBIDDEN_PARAMS = frozenset(
    {
        _API_KEY_PARAM,
        "signature",
        "timestamp",
        "recvWindow",
        "api_key",
        "secret",
        "secret_key",
    }
)
_FORBIDDEN_PARAMS_LOWER = frozenset({k.lower() for k in FORBIDDEN_PARAMS})
# Default matches runtime.max_concurrent_rest_requests; override via configure_rest_concurrency().
_REST_GLOBAL_SEMAPHORE_STATE: list[asyncio.Semaphore] = [asyncio.Semaphore(3)]


def rest_global_semaphore() -> asyncio.Semaphore:
    """Return the process-wide REST concurrency gate (reconfigured at runtime)."""
    return _REST_GLOBAL_SEMAPHORE_STATE[0]


_REST_WEIGHT_SOFT_LIMIT = REST_WEIGHT_SOFT_LIMIT
_REST_WEIGHT_PACE_LIMIT = REST_WEIGHT_PACE_LIMIT
_REST_WEIGHT_HARD_LIMIT = REST_WEIGHT_HARD_LIMIT
_REST_WEIGHT_CRITICAL_LIMIT = REST_WEIGHT_CRITICAL_LIMIT


def configure_rest_concurrency(max_concurrent: int) -> None:
    """Align global REST gate with BotSettings (Binance weight budget is separate)."""
    limit = max(1, min(int(max_concurrent), 20))
    _REST_GLOBAL_SEMAPHORE_STATE[0] = asyncio.Semaphore(limit)
    LOG.info("rest_concurrency_configured | max_concurrent=%d", limit)


_FUTURES_DATA_IP_LIMIT_WINDOW_S = 300.0
_FUTURES_DATA_IP_LIMIT_OFFICIAL_MAX = 1000
_FUTURES_DATA_IP_LIMIT_DEFAULT = 300
_HTTP_CONNECTOR_LIMIT = 50
_CACHE_TTL = {
    "klines_5m": 300,
    "klines_15m": 900,
    "klines_1h": 3900,
    "klines_4h": 14400,
    "open_interest": 600,
    "open_interest_change": 600,
    "long_short_ratio": 600,
    "taker_ratio": 600,
    "global_ls_ratio": 600,
    "funding_rate": 300,
    "funding_history": 1800,
    "basis": 600,
    "continuous_klines": 1800,
    "mark_price_klines": 1800,
    "index_price_klines": 1800,
    "book_ticker": 5,
    "order_book_depth": 5,
}
_PERIOD_WINDOW_SECONDS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}
_KLINE_COLUMNS = (
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
)
_KLINE_FRAME_SCHEMA = {
    "time": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "close_time": pl.Datetime("us", "UTC"),
    "quote_volume": pl.Float64,
    "num_trades": pl.Int64,
    "taker_buy_base_volume": pl.Float64,
    "taker_buy_quote_volume": pl.Float64,
    "open_time": pl.Datetime("us", "UTC"),
}
_ENDPOINT_WEIGHTS = {
    "test_connectivity": 1,
    "check_server_time": 1,
    "exchange_information": 1,
    "ticker24hr_price_change_statistics": 40,
    "symbol_order_book_ticker": 2,
    "order_book_depth": 2,
    "compressed_aggregate_trades_list": 20,
    "open_interest": 1,
    "open_interest_statistics": 0,
    "top_trader_long_short_ratio_accounts": 0,
    "top_trader_long_short_ratio_positions": 0,
    "global_long_short_account_ratio": 0,
    "taker_long_short_ratio": 0,
    "basis": 0,
    "premium_index": 1,
    "funding_rate_history": 1,
    "funding_info": 1,
    "continuous_kline_candlestick_data": 1,
    "mark_price_kline_data": 1,
    "index_price_kline_data": 1,
}
_FUTURES_DATA_REQUEST_LIMITED_OPS = frozenset(
    {
        "open_interest_statistics",
        "top_trader_long_short_ratio_accounts",
        "top_trader_long_short_ratio_positions",
        "global_long_short_account_ratio",
        "taker_long_short_ratio",
        "basis",
        "funding_rate_history",
    }
)
_DEFAULT_KLINE_FETCH_LIMIT = 500
_DEFAULT_ORDER_BOOK_DEPTH_LIMIT = 20
_VALID_ORDER_BOOK_DEPTH_LIMITS = frozenset({5, 10, 20, 50, 100, 500, 1000})
_FALLBACK_TIMEOUT_DEBUG_OPERATIONS = frozenset({"symbol_order_book_ticker", "order_book_depth"})
_REST_TIMEOUT_WARNING_OPERATIONS = frozenset({"kline_candlestick_data", "symbol_order_book_ticker"})


@dataclass(frozen=True, slots=True)
class _PublicEndpointSpec:
    """Spec for a Binance public REST endpoint."""

    path: str
    source: str = "rest"
    weight_key: str | None = None
    ip_limited: bool = False


def _build_endpoint_registry() -> dict[str, _PublicEndpointSpec]:
    registry: dict[str, _PublicEndpointSpec] = {}
    fapi1 = "/fapi/v1"
    fdata = "/futures/data"

    # Map operation names to API paths
    # Based on Binance USD-M Futures API
    op_paths: dict[str, str] = {
        "test_connectivity": f"{fapi1}/ping",
        "check_server_time": f"{fapi1}/time",
        "exchange_information": f"{fapi1}/exchangeInfo",
        "ticker24hr_price_change_statistics": f"{fapi1}/ticker/24hr",
        "kline_candlestick_data": f"{fapi1}/klines",
        "symbol_order_book_ticker": f"{fapi1}/ticker/bookTicker",
        "order_book_depth": f"{fapi1}/depth",
        "compressed_aggregate_trades_list": f"{fapi1}/aggTrades",
        "premium_index": f"{fapi1}/premiumIndex",
        "continuous_kline_candlestick_data": f"{fapi1}/continuousKlines",
        "mark_price_kline_data": f"{fapi1}/markPriceKlines",
        "index_price_kline_data": f"{fapi1}/indexPriceKlines",
        "open_interest": f"{fapi1}/openInterest",
        "funding_rate_history": f"{fapi1}/fundingRate",
        "funding_info": f"{fapi1}/fundingInfo",
        "open_interest_statistics": f"{fdata}/openInterestHist",
        "top_trader_long_short_ratio_accounts": f"{fdata}/topLongShortAccountRatio",
        "top_trader_long_short_ratio_positions": f"{fdata}/topLongShortPositionRatio",
        "global_long_short_account_ratio": f"{fdata}/globalLongShortAccountRatio",
        "taker_long_short_ratio": f"{fdata}/takerlongshortRatio",
        "basis": f"{fdata}/basis",
    }

    for op_name, path in op_paths.items():
        ip_limited = op_name in _FUTURES_DATA_REQUEST_LIMITED_OPS
        registry[op_name] = _PublicEndpointSpec(path, ip_limited=ip_limited)

    return registry


_PUBLIC_ENDPOINT_REGISTRY = _build_endpoint_registry()
_PUBLIC_PATH_PREFIXES = ("/fapi/v1/", "/futures/data/")
_ALLOWED_PUBLIC_REST_PATHS = ("/fapi/v1/", "/fapi/v2/", "/futures/data/")
_FORBIDDEN_PUBLIC_PATH_MARKERS = (
    "/private",
    "listenkey",
    "/ws-api",
    "/sapi",
    "/papi",
    "/dapi",
    "signature",
    "timestamp=",
    "api_key=",
    "apikey=",
)
_VALID_INTERVALS = frozenset(
    {
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
    }
)


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


def _validate_public_endpoint_registry() -> None:
    for operation, spec in _PUBLIC_ENDPOINT_REGISTRY.items():
        if not spec.path.startswith(_PUBLIC_PATH_PREFIXES):
            msg = f"unsupported public endpoint path for {operation}: {spec.path}"
            raise ValueError(msg)


class BinanceFuturesMarketData:
    def __init__(
        self,
        *,
        binance_client: BinanceClientImpl,
        ws_manager: FuturesWSManager | None = None,
    ) -> None:
        self._binance_client = binance_client
        self._ws: FuturesWSManager | None = ws_manager

    # Delegation methods to the BinanceClient
    async def fetch_exchange_symbols(self) -> list[SymbolMeta]:
        return await self._binance_client.fetch_exchange_symbols()

    async def fetch_ticker_24h(self) -> list[dict[str, float | str]]:
        return await self._binance_client.fetch_ticker_24h()

    async def fetch_klines(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        return await self._binance_client.fetch_klines(symbol, interval, limit=limit)

    async def fetch_klines_between(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1500,
    ) -> pl.DataFrame:
        return await self._binance_client.fetch_klines_between(
            symbol,
            interval,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            limit=limit,
        )

    async def fetch_klines_cached(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        return await self._binance_client.fetch_klines_cached(symbol, interval, limit=limit)

    def get_cached_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int,
        max_age_s: float | None = None,
    ) -> pl.DataFrame | None:
        return self._binance_client.get_cached_klines(
            symbol, interval, limit=limit, max_age_s=max_age_s
        )

    async def fetch_continuous_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> pl.DataFrame:
        return await self._binance_client.fetch_continuous_klines(symbol, interval, limit=limit)

    async def fetch_mark_price_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> pl.DataFrame:
        return await self._binance_client.fetch_mark_price_klines(symbol, interval, limit=limit)

    async def fetch_index_price_klines(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> pl.DataFrame:
        return await self._binance_client.fetch_index_price_klines(symbol, interval, limit=limit)

    async def fetch_priority_history_bundle(
        self,
        symbol: str,
        *,
        intervals: tuple[str, ...] = ("15m", "1h", "4h"),
        limit: int = 300,
    ) -> dict[str, pl.DataFrame]:
        return await self._binance_client.fetch_priority_history_bundle(
            symbol, intervals=intervals, limit=limit
        )

    async def fetch_order_book_depth_snapshot(
        self, symbol: str, *, limit: int = 20
    ) -> dict[str, float | None]:
        return await self._binance_client.fetch_order_book_depth_snapshot(symbol, limit=limit)

    async def _fetch_book_ticker_rest_detail(self, symbol: str) -> dict[str, float | None]:
        return await self._binance_client._fetch_book_ticker_rest_detail(symbol)

    async def _call_public_http_json(
        self,
        operation: str,
        *,
        params: dict[str, Any] | None = None,
        symbol: str | None = None,
    ) -> Any:
        return await self._binance_client._call_public_http_json(
            operation, params=params, symbol=symbol
        )

    async def _get_http_session(self) -> Any:
        return await self._binance_client._get_http_session()

    async def fetch_funding_rate(self, symbol: str) -> float | None:
        return await self._binance_client.fetch_funding_rate(symbol)

    async def fetch_premium_index_all(self) -> dict[str, dict[str, float]]:
        return await self._binance_client.fetch_premium_index_all()

    async def fetch_open_interest(self, symbol: str) -> float | None:
        return await self._binance_client.fetch_open_interest(symbol)

    async def fetch_open_interest_change(self, symbol: str, *, period: str = "1h") -> float | None:
        return await self._binance_client.fetch_open_interest_change(symbol, period=period)

    async def fetch_long_short_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        return await self._binance_client.fetch_long_short_ratio(symbol, period=period)

    async def fetch_top_position_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        return await self._binance_client.fetch_top_position_ls_ratio(symbol, period=period)

    async def fetch_taker_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        return await self._binance_client.fetch_taker_ratio(symbol, period=period)

    async def fetch_global_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        return await self._binance_client.fetch_global_ls_ratio(symbol, period=period)

    async def fetch_funding_rate_history(
        self, symbol: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return await self._binance_client.fetch_funding_rate_history(symbol, limit=limit)

    async def fetch_agg_trade_snapshot(self, symbol: str, *, limit: int = 100) -> AggTradeSnapshot:
        return await self._binance_client.fetch_agg_trade_snapshot(symbol, limit=limit)

    async def fetch_agg_trades(
        self,
        symbol: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        page_limit: int,
        page_size: int,
    ) -> tuple[list[AggTrade], bool]:
        return await self._binance_client.fetch_agg_trades(
            symbol,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            page_limit=page_limit,
            page_size=page_size,
        )

    async def fetch_book_ticker(self, symbol: str) -> tuple[float | None, float | None]:
        return await self._binance_client.fetch_book_ticker(symbol)

    async def close(self) -> None:
        await self._binance_client.close()
        if self._ws is not None:
            stop = getattr(self._ws, "stop", None)
            if callable(stop):
                await stop()

    def state_snapshot(self) -> dict[str, float | int | str | None]:
        # Combine state from both clients
        binance_state = self._binance_client.state_snapshot()
        ws_state: dict[str, float | int | str | None] = {}
        if self._ws is not None:
            # Assuming WS manager has a similar state method
            ws_state = getattr(self._ws, "state_snapshot", dict)()

        # Merge states (binance state takes precedence for conflicts)
        state = {**ws_state, **binance_state}
        state["ws_manager_available"] = (
            self._ws is not None and getattr(self._ws, "is_connected", lambda: False)()
        )
        return state

    async def preflight_check(self) -> None:
        await self._binance_client.preflight_check()
        if self._ws is not None:
            preflight = getattr(self._ws, "preflight_check", None)
            if callable(preflight):
                await preflight()

    # Cache accessors - delegate to binance client
    def get_cached_oi_change(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        return self._binance_client.get_cached_oi_change(symbol, period, max_age_s)

    def get_cached_open_interest(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        return self._binance_client.get_cached_open_interest(symbol, max_age_s)

    def get_cached_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        return self._binance_client.get_cached_ls_ratio(symbol, period, max_age_s)

    def get_cached_funding_rate(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        return self._binance_client.get_cached_funding_rate(symbol, max_age_s)

    def get_cached_premium_index(
        self, symbol: str, max_age_s: float = 300.0
    ) -> dict[str, float] | None:
        return self._binance_client.get_cached_premium_index(symbol, max_age_s)

    def get_cached_top_position_ls_ratio(
        self,
        symbol: str,
        period: str = "1h",
        max_age_s: float = 1800.0,
    ) -> float | None:
        return self._binance_client.get_cached_top_position_ls_ratio(symbol, period, max_age_s)

    def get_cached_taker_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        return self._binance_client.get_cached_taker_ratio(symbol, period, max_age_s)

    def get_cached_global_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        return self._binance_client.get_cached_global_ls_ratio(symbol, period, max_age_s)

    def get_cached_basis_stats(
        self,
        symbol: str,
        period: str = "1h",
        max_age_s: float = 1800.0,
    ) -> dict[str, float | None] | None:
        return self._binance_client.get_cached_basis_stats(symbol, period, max_age_s)

    def update_basis_from_websocket(
        self,
        symbol: str,
        mark_price: float,
        index_price: float | None = None,
        period: str = "5m",
    ) -> dict[str, float | None] | None:
        return self._binance_client.update_basis_from_websocket(
            symbol, mark_price, index_price, period
        )

    def get_cached_funding_trend(self, symbol: str, max_age_s: float = 1800.0) -> str | None:
        return self._binance_client.get_cached_funding_trend(symbol, max_age_s)

    def get_cached_funding_recent_extreme(
        self,
        symbol: str,
        *,
        max_age_hours: float = 48.0,
        max_cache_age_s: float = 1800.0,
    ) -> tuple[float, float] | None:
        return self._binance_client.get_cached_funding_recent_extreme(
            symbol,
            max_age_hours=max_age_hours,
            max_cache_age_s=max_cache_age_s,
        )

    async def fetch_basis(self, symbol: str, *, period: str = "1h", limit: int = 3) -> float | None:
        return await self._binance_client.fetch_basis(symbol, period=period, limit=limit)

    def get_cached_basis(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        return self._binance_client.get_cached_basis(symbol, period, max_age_s)

    def get_rest_enrichment_stale_flags(
        self,
        symbol: str,
        *,
        ls_period: str = "1h",
        basis_period: str = "1h",
        basis_stats_period: str = "5m",
    ) -> tuple[str, ...]:
        return self._binance_client.get_rest_enrichment_stale_flags(
            symbol,
            ls_period=ls_period,
            basis_period=basis_period,
            basis_stats_period=basis_stats_period,
        )

    async def fetch_symbol_frames(self, symbol: str) -> SymbolFrames:
        return await self._binance_client.fetch_symbol_frames(symbol)

from __future__ import annotations
import asyncio
import collections
import contextlib
import json as _stdlib_json
import logging
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import polars as pl
from websockets import exceptions as ws_exceptions
from bot.runtime.errors import DEFENSIVE_EXC
from bot.market.network_proxy import apply_proxy_env, mask_proxy_url, normalize_proxy_url
from ..domain.events import KlineCloseEvent
from ..domain.schemas import AggTrade, AggTradeSnapshot, SymbolFrames
from .data import MarketDataUnavailable
from .universe import build_shortlist
import random
from typing import Any
import json
from bot.market.subscription_planner import plan_subscription_budget
import socket
import websockets
from bot.market.network_proxy import websockets_connect_kwargs
from ..domain.events import BookTickerEvent
from ..domain.schemas import AggTrade

if TYPE_CHECKING:
    from types import ModuleType

# Use orjson for faster JSON parsing if available
_json: ModuleType
try:
    import orjson as _orjson

    _json = _orjson

    _USE_ORJSON = True
except ImportError:
    _json = _stdlib_json

    _USE_ORJSON = False

if TYPE_CHECKING:
    from ..core.event_bus import EventBus
    from ..domain.config import WSConfig
    from .data import BinanceFuturesMarketData


_BACKOFF_RESET_AFTER_SECONDS = 90.0
_PROACTIVE_RECONNECT_AFTER_SECONDS = 23 * 3600 + 50 * 60
_HEALTH_CHECK_INTERVAL_SECONDS = 30.0

# Binance WebSocket limits (from official docs)
_MAX_STREAMS_PER_CONNECTION = 300  # Conservative limit (docs say 1024, but 10 msg/sec limit)
_MAX_INCOMING_MSG_PER_SECOND = 8  # Below Binance's 10 msg/sec limit with safety margin
_MAX_SUBSCRIBE_MSG_PER_SECOND = 4  # JSON control messages limit is 5 msg/sec

_INTERVAL_SECONDS: dict[str, int] = {
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
LOG = logging.getLogger("bot.ws_manager")
JsonDict = dict[str, Any]
KlineCloseCallback = Callable[[str, str, int], Coroutine[Any, Any, None]]
AggTradeCallback = Callable[[str, float, datetime], Coroutine[Any, Any, None]]
ReconnectCallback = Callable[[], Coroutine[Any, Any, None]]
_HIGH_EVENT_LATENCY_MS = 5_000.0
_LATENCY_WARNING_INTERVAL_SECONDS = 60.0
_SHORT_DISCONNECT_BACKFILL_GRACE_SECONDS = 30.0
_LATENCY_WARNING_EVENTS = {"kline", "bookTicker", "aggTrade"}
_STALE_DROP_EVENTS = {
    "bookTicker",
    "depthUpdate",
    "aggTrade",
    "24hrTicker",
    "miniTicker",
    "markPriceUpdate",
}
_WS_PUBLIC = "public"
_WS_MARKET = "market"
_WS_ENDPOINTS = (_WS_PUBLIC, _WS_MARKET)
# Global Binance arrays must not sit behind the kline backlog — stale E timestamps get dropped.
_GLOBAL_MARKET_STREAM_PREFIXES = (
    "!ticker@arr",
    "!markprice@arr",
    "!miniticker@arr",
)


def _is_global_market_stream(stream: str) -> bool:
    normalized = str(stream or "").strip().lower()
    return any(normalized.startswith(prefix) for prefix in _GLOBAL_MARKET_STREAM_PREFIXES)


class RateLimiter:
    """Rate limiter for incoming WebSocket messages (Binance limit: 10 msg/sec)."""

    def __init__(self, max_per_second: int = _MAX_INCOMING_MSG_PER_SECOND) -> None:
        self.max_per_second = max_per_second
        self._timestamps: collections.deque[float] = collections.deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Try to acquire permission to process a message. Returns True if allowed."""
        async with self._lock:
            now = time.monotonic()
            # Remove timestamps older than 1 second
            while self._timestamps and now - self._timestamps[0] > 1.0:
                self._timestamps.popleft()

            if len(self._timestamps) < self.max_per_second:
                self._timestamps.append(now)
                return True
            return False

    async def wait_for_slot(self) -> None:
        """Wait until a slot is available."""
        while not await self.acquire():
            ready = asyncio.Event()
            loop = asyncio.get_running_loop()
            if self._timestamps:
                delay = max(0.01, 1.0 - (time.monotonic() - self._timestamps[0]))
            else:
                delay = 0.05
            loop.call_later(delay, ready.set)
            await ready.wait()


class MessageBuffer:
    """Buffer for WebSocket messages with backpressure handling."""

    def __init__(self, maxsize: int = 10000) -> None:
        self._buffer: asyncio.Queue[JsonDict] = asyncio.Queue(maxsize=maxsize)
        self._dropped_count = 0
        self._processed_count = 0
        self._last_compaction_log_count = 0
        self._protected_kline_drop_count = 0

    @staticmethod
    def _message_priority(msg: JsonDict) -> int:
        data = msg.get("data")
        stream = str(msg.get("stream") or "").lower()
        if isinstance(data, list) and _is_global_market_stream(stream):
            return 95
        if isinstance(data, dict):
            event_type = data.get("e")
            if event_type == "kline":
                kline = data.get("k")
                if isinstance(kline, dict) and kline.get("x"):
                    return 100
            if event_type == "forceOrder":
                return 90
            if event_type == "bookTicker":
                return 85
            if event_type == "markPriceUpdate":
                return 95
            if event_type == "aggTrade":
                return 40
        return 10

    @staticmethod
    def _closed_kline_timestamp(msg: JsonDict) -> float | None:
        data = msg.get("data")
        if not isinstance(data, dict) or data.get("e") != "kline":
            return None
        kline = data.get("k")
        if not isinstance(kline, dict) or not kline.get("x"):
            return None
        try:
            open_time = kline.get("t")
            if open_time is None:
                return None
            return float(open_time)
        except (TypeError, ValueError):
            return None

    def _drop_oldest_batch(self) -> int:
        """Drop lower-value queued messages first when clearing backpressure."""
        maxsize = max(1, self._buffer.maxsize)
        target = max(1, maxsize // 2)
        batch: list[JsonDict] = []
        for _ in range(target):
            try:
                batch.append(self._buffer.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not batch:
            return 0

        closed_kline_indexes = [
            idx for idx, msg in enumerate(batch) if self._message_priority(msg) == 100
        ]
        non_closed_indexes = [
            idx for idx, msg in enumerate(batch) if self._message_priority(msg) < 100
        ]
        if closed_kline_indexes and non_closed_indexes:
            LOG.debug(
                "message buffer backpressure with protected closed klines | protected=%d "
                "queued=%d",
                len(closed_kline_indexes),
                len(batch),
            )
        elif closed_kline_indexes:
            self._protected_kline_drop_count += len(closed_kline_indexes)

        # Keep closed klines ahead of all other message types. Under mixed
        # pressure, only non-closed messages can be selected for eviction.
        if non_closed_indexes:
            lowest_priority = min(self._message_priority(batch[idx]) for idx in non_closed_indexes)
            drop_indexes = {
                idx
                for idx in non_closed_indexes
                if self._message_priority(batch[idx]) == lowest_priority
            }
        else:
            # The sampled window is all closed klines. Dropping the oldest closed
            # candle is preferable to rejecting the incoming event and stalling
            # the queue forever, but the counter/log above makes this visible.
            drop_index, _ = min(
                enumerate(batch),
                key=lambda item: (
                    self._closed_kline_timestamp(item[1])
                    if self._closed_kline_timestamp(item[1]) is not None
                    else float("inf"),
                    item[0],
                ),
            )
            drop_indexes = {drop_index}
            LOG.warning(
                "message buffer forced to drop a closed kline (all high-priority) | "
                "dropped_total=%d",
                self._dropped_count + 1,
            )

        dropped = 0
        for idx, msg in enumerate(batch):
            if idx in drop_indexes:
                dropped += 1
                continue
            with contextlib.suppress(asyncio.QueueFull):
                self._buffer.put_nowait(msg)
        return dropped

    async def put(self, msg: JsonDict) -> bool:
        """Add message to buffer, dropping oldest queued data under backpressure."""
        try:
            self._buffer.put_nowait(msg)
        except asyncio.QueueFull:
            dropped = self._drop_oldest_batch()
            if dropped <= 0:
                self._dropped_count += 1
                LOG.debug(
                    "message buffer compact failed | dropped_oldest=%d processed=%d",
                    self._dropped_count,
                    self._processed_count,
                )
                return False
            self._dropped_count += dropped
            try:
                self._buffer.put_nowait(msg)
            except asyncio.QueueFull:
                self._dropped_count += 1
                buffered = False
            else:
                buffered = True
            if self._dropped_count - self._last_compaction_log_count >= 10000:
                self._last_compaction_log_count = self._dropped_count
                LOG.debug(
                    "message buffer compacted | dropped_oldest=%d processed=%d size=%d",
                    self._dropped_count,
                    self._processed_count,
                    self._buffer.qsize(),
                )
            return buffered
        else:
            return True

    async def get(self) -> JsonDict | None:
        """Get message from buffer. Returns None if empty."""
        try:
            msg = self._buffer.get_nowait()
            self._processed_count += 1
        except asyncio.QueueEmpty:
            return None
        else:
            return msg

    def get_stats(self) -> dict[str, int]:
        """Return buffer statistics."""
        return {
            "size": self._buffer.qsize(),
            "maxsize": self._buffer.maxsize,
            "dropped": self._dropped_count,
            "processed": self._processed_count,
            "protected_kline_drop_count": self._protected_kline_drop_count,
        }


def _ws_kline_to_row(k: JsonDict) -> JsonDict:
    return {
        "time": datetime.fromtimestamp(int(k["t"]) / 1000.0, tz=UTC),
        "open": float(k["o"]),
        "high": float(k["h"]),
        "low": float(k["l"]),
        "close": float(k["c"]),
        "volume": float(k["v"]),
        "close_time": datetime.fromtimestamp(int(k["T"]) / 1000.0, tz=UTC),
        "quote_volume": float(k["q"]),
        "num_trades": int(k["n"]),
        "taker_buy_base_volume": float(k["V"]),
        "taker_buy_quote_volume": float(k["Q"]),
    }


def _resolve_message_buffer_maxsize(cfg: WSConfig, *, symbol_count: int = 50) -> int:
    configured = int(getattr(cfg, "message_buffer_maxsize", 0) or 0)
    if configured > 0:
        return configured
    return max(10_000, min(200_000, symbol_count * 1200))


# --- inlined from ws_enrichment.py ---
def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def depth_imbalance_from_book(
    *,
    bid_qty: float | None,
    ask_qty: float | None,
    delta_ratio: float | None,
) -> float | None:
    """Return top-of-book depth imbalance, falling back to signed trade flow."""
    if bid_qty is not None and ask_qty is not None and bid_qty >= 0 and ask_qty >= 0:
        total = bid_qty + ask_qty
        if total > 0.0:
            return round(_clamp((bid_qty - ask_qty) / total), 4)
    if delta_ratio is None:
        return None
    return round(_clamp(float(delta_ratio)), 4)


def microprice_bias_from_book(
    *,
    bid: float | None,
    ask: float | None,
    bid_qty: float | None = None,
    ask_qty: float | None = None,
    delta_ratio: float | None,
) -> float | None:
    """Return signed microprice bias from L1 book, falling back to trade flow."""
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    spread = ask - bid
    mid = (bid + ask) / 2.0
    if mid <= 0 or spread <= 0:
        return None
    if bid_qty is not None and ask_qty is not None and bid_qty >= 0 and ask_qty >= 0:
        total_qty = bid_qty + ask_qty
        if total_qty > 0.0:
            microprice = ((ask * bid_qty) + (bid * ask_qty)) / total_qty
            half_spread = spread / 2.0
            if half_spread > 0.0:
                return round(_clamp((microprice - mid) / half_spread), 4)
    if delta_ratio is None:
        return None
    return round(_clamp(float(delta_ratio)), 4)

# --- inlined from ws_reconnect.py ---
_BACKOFF_RESET_AFTER_SECONDS = 90.0


def compute_disconnect_delay(
    manager: Any,
    *,
    endpoint: str,
    url: str,
    exc: Exception,
    elapsed: float,
    delay: float,
) -> float:
    """Update streak/reconnect metadata and return next retry delay."""
    error_text = str(exc).lower()
    keepalive_timeout = "keepalive ping timeout" in error_text
    if elapsed < _BACKOFF_RESET_AFTER_SECONDS and not keepalive_timeout:
        manager._short_lived_streak += 1
    else:
        manager._short_lived_streak = 0

    close_detail = ""
    if isinstance(exc, ws_exceptions.ConnectionClosed):
        close_code = exc.rcvd.code if exc.rcvd else "not_received"
        close_reason = repr(exc.rcvd.reason) if exc.rcvd else ""
        close_detail = f" code={close_code} reason={close_reason}"

    if keepalive_timeout:
        min_delay = 1.0
    elif manager._short_lived_streak >= 8:
        min_delay = 300.0
    elif manager._short_lived_streak >= 5:
        min_delay = 30.0
    elif manager._short_lived_streak >= 3:
        min_delay = 5.0
    else:
        min_delay = 1.0

    next_delay = min(300.0, max(1.0, delay, min_delay))
    next_delay = min(
        300.0,
        next_delay + random.uniform(0.0, min(0.5, next_delay * 0.1)),
    )  # seconds: reconnect backoff stays between 1s and 5min.

    manager._last_reconnect_reason = f"{endpoint}:{exc}"
    manager._last_reconnect_reason_by_endpoint[endpoint] = str(exc)
    level = logging.INFO
    log_reason = "keepalive_ping_timeout" if keepalive_timeout else str(exc)
    LOG.log(
        level,
        (
            "ws disconnected | endpoint=%s url=%s reason=%s close_detail=%s "
            "uptime=%.1fs retry_in=%.1fs streak=%d"
        ),
        endpoint,
        url,
        log_reason,
        close_detail,
        elapsed,
        next_delay,
        manager._short_lived_streak,
    )
    return next_delay

# --- inlined from ws_health.py ---
async def evaluate_endpoint_health(manager: Any, ws: Any, endpoint: str) -> bool:
    """Evaluate endpoint-specific health checks.

    Returns True if a reconnect was triggered (ws.close called), else False.
    """
    max_silence = float(
        getattr(
            manager._cfg,
            "silence_timeout_seconds",
            getattr(manager._cfg, "health_check_silence_seconds", 60.0),
        )
    )
    last_message_ts = manager._last_message_ts_by_endpoint.get(endpoint, 0.0)
    if last_message_ts > 0.0:
        last_message_age = time.monotonic() - last_message_ts
        if last_message_age > max_silence:
            LOG.info(
                "ws endpoint silence exceeded | endpoint=%s age=%.1fs max=%.1fs",
                endpoint,
                last_message_age,
                max_silence,
            )
            await ws.close()
            return True
    grace_seconds = max(
        60.0,
        float(getattr(manager._cfg, "market_reconnect_grace_seconds", 60.0)),
    )
    if endpoint == "market":
        connected_at = manager._connected_at_by_endpoint.get(endpoint, 0.0)
        if connected_at > 0.0:
            recovery_age = time.monotonic() - connected_at
            if recovery_age < grace_seconds:
                return False
            if recovery_age >= grace_seconds:
                snapshot = manager.state_snapshot()
                if (
                    int(snapshot.get("fresh_tickers") or 0) == 0
                    or int(snapshot.get("fresh_mark_prices") or 0) == 0
                ):
                    LOG.error(
                        (
                            "ws market recovery failed | endpoint=%s age=%.1fs "
                            "fresh_tickers=%s fresh_mark_prices=%s - forcing reconnect"
                        ),
                        endpoint,
                        recovery_age,
                        snapshot.get("fresh_tickers"),
                        snapshot.get("fresh_mark_prices"),
                    )
                    await ws.close()
                    return True

        stale_streams = manager._stale_kline_streams()
        if stale_streams:
            preview = stale_streams[:3]
            stale_symbols = list({s.split(":")[0] for s in stale_streams})
            LOG.info(
                (
                    "ws stale kline data | endpoint=%s streams=%d sample=%s - "
                    "backfilling (not reconnecting)"
                ),
                endpoint,
                len(stale_streams),
                preview,
            )
            task = asyncio.create_task(manager._backfill(stale_symbols))
            manager._backfill_tasks.add(task)
            task.add_done_callback(manager._backfill_tasks.discard)
        return False

    if endpoint == "public":
        fresh_books = sum(
            1
            for sym, ts in manager._book_update_times.items()
            if sym in manager._symbols
            and time.monotonic() - ts <= manager._cfg.market_ticker_freshness_seconds
        )
        if manager._symbols and manager._cfg.subscribe_book_ticker and fresh_books == 0:
            connected_at = manager._connected_at_by_endpoint.get(endpoint, 0.0)
            if connected_at > 0.0 and time.monotonic() - connected_at >= grace_seconds:
                LOG.error(
                    (
                        "ws public recovery failed | endpoint=%s fresh_book_tickers=0 - "
                        "forcing reconnect"
                    ),
                    endpoint,
                )
                await ws.close()
                return True
    return False


async def monitor_connection_silence(manager: Any, ws: Any, endpoint: str) -> bool:
    """Check generic silence timeout and force reconnect when needed."""
    streams = manager._intended_streams_by_endpoint.get(endpoint, set())
    silence_limit = manager._cfg.health_check_silence_seconds
    if endpoint == "public" and any(
        "@bookticker" in str(stream).lower() or "@depth" in str(stream).lower()
        for stream in streams
    ):
        silence_limit = min(float(silence_limit), 15.0)
    last_message_ts = manager._last_message_ts_by_endpoint.get(endpoint, 0.0)
    if last_message_ts == 0.0:
        return False
    connected_map = getattr(manager, "_connected_at_by_endpoint", {})
    connected_at = connected_map.get(endpoint, 0.0) if isinstance(connected_map, dict) else 0.0
    grace_seconds = max(
        60.0,
        float(getattr(manager._cfg, "market_reconnect_grace_seconds", 60.0)),
    )
    if connected_at > 0.0 and time.monotonic() - connected_at < grace_seconds:
        return False
    silence = time.monotonic() - last_message_ts
    if silence > silence_limit and streams:
        LOG.info(
            "ws health: no message for %.0fs with %d streams - forcing reconnect | endpoint=%s",
            silence,
            len(streams),
            endpoint,
        )
        await ws.close()
        return True
    return False

# --- inlined from ws_subscriptions.py ---
DEFAULT_MAX_STREAMS_PER_CONNECTION = 300
FORBIDDEN_STREAM_SUFFIXES = ("@userData", "@account", "@balanceUpdate")


def _normalized_symbols(symbols: list[str]) -> list[str]:
    normalized = [str(symbol or "").strip().lower() for symbol in symbols]
    return list(dict.fromkeys(symbol for symbol in normalized if symbol))


def base_streams_for_symbols(manager: Any, symbols: list[str]) -> list[str]:
    return [
        f"{sym}@kline_{interval}"
        for sym in _normalized_symbols(symbols)
        for interval in manager._cfg.kline_intervals
    ]


def public_streams_for_symbols(manager: Any, symbols: list[str]) -> list[str]:
    if not manager._cfg.subscribe_book_ticker:
        return []
    streams = [f"{symbol}@bookTicker" for symbol in _normalized_symbols(symbols)]
    return [
        stream
        for stream in streams
        if not any(stream.endswith(forbidden) for forbidden in FORBIDDEN_STREAM_SUFFIXES)
    ]


def tracked_depth_streams(manager: Any, symbols: list[str]) -> list[str]:
    if not getattr(manager._cfg, "subscribe_depth", False):
        return []
    limit = int(getattr(manager._cfg, "depth_symbol_limit", 0) or 0)
    if limit <= 0:
        return []
    levels = int(getattr(manager._cfg, "depth_levels", 20) or 20)
    if levels not in {5, 10, 20}:
        levels = 20
    speed = str(getattr(manager._cfg, "depth_speed", "500ms") or "500ms").lower()
    suffix = "" if speed == "250ms" else f"@{speed}"
    return [f"{symbol}@depth{levels}{suffix}" for symbol in _normalized_symbols(symbols)[:limit]]


def stream_endpoint_class(stream: str) -> str:
    normalized = str(stream or "").strip().lower()
    if any(
        token in normalized
        for token in ("listenkey", "/private", "userdatastream", "@account", "@order")
    ):
        msg = f"private/auth websocket streams are not allowed: {stream}"
        raise ValueError(msg)
    if "@bookticker" in normalized or "@depth" in normalized:
        return "public"
    allowed_market = (
        "@kline_",
        "@aggtrade",
        "@markprice",
        "!markprice@arr@1s",
        "!markprice@arr",
        "!ticker@arr",
        "!miniticker@arr",
        "!forceorder@arr",
    )
    if any(token in normalized for token in allowed_market):
        return "market"
    msg = f"unsupported public websocket stream: {stream}"
    raise ValueError(msg)


def tracked_agg_trade_streams(manager: Any, symbols: list[str]) -> list[str]:
    if not manager._should_subscribe_agg_trade():
        return []
    return [f"{symbol}@aggTrade" for symbol in _normalized_symbols(symbols)]


def global_streams(manager: Any) -> list[str]:
    if not manager._cfg.subscribe_market_streams:
        return []
    streams = [
        "!ticker@arr",
        "!markPrice@arr@1s",
        "!forceOrder@arr",
    ]
    if not manager.is_ticker_cache_warm():
        streams.append("!miniTicker@arr")
    return streams


def recompute_intended_streams(manager: Any) -> None:
    symbols = list(manager._symbols)
    tracked = list(manager._tracked_symbols or manager._symbols)
    budget_plan = plan_subscription_budget(symbols, tracked, ws=manager._cfg)
    depth_symbols = list(budget_plan.depth_symbols) or tracked
    agg_symbols = list(budget_plan.agg_trade_symbols) or tracked
    manager._subscription_budget = budget_plan

    public_streams = set(public_streams_for_symbols(manager, manager._symbols))
    public_streams.update(tracked_depth_streams(manager, depth_symbols))
    market_streams = set(base_streams_for_symbols(manager, manager._symbols))
    market_streams.update(tracked_agg_trade_streams(manager, agg_symbols))
    if manager._symbols or manager._cfg.subscribe_market_streams:
        market_streams.update(global_streams(manager))
    manager._intended_streams_by_endpoint["public"] = public_streams
    manager._intended_streams_by_endpoint["market"] = market_streams
    manager._intended_streams = set().union(public_streams, market_streams)
    validate_endpoint_stream_limits(manager)
    LOG.debug(
        "subscription budget | market=%d public=%d depth=%d agg=%d limit=%d",
        budget_plan.total_market,
        budget_plan.total_public,
        budget_plan.depth_streams,
        budget_plan.agg_trade_streams,
        budget_plan.budget_limit,
    )


def validate_endpoint_stream_limits(manager: Any) -> None:
    max_streams = int(
        getattr(manager, "_max_streams_per_connection", DEFAULT_MAX_STREAMS_PER_CONNECTION)
    )
    for endpoint, streams in manager._intended_streams_by_endpoint.items():
        if len(streams) > max_streams:
            msg = (
                "websocket stream count exceeds configured safety limit "
                f"| endpoint={endpoint} streams={len(streams)} max={max_streams}"
            )
            raise ValueError(msg)


async def send_subscription_command(
    manager: Any,
    endpoint: str,
    method: str,
    streams: list[str],
) -> None:
    if not streams:
        return
    ws_conn = manager._ws_conns.get(endpoint)
    if ws_conn is None:
        return
    streams = sorted(dict.fromkeys(str(stream or "").strip() for stream in streams if stream))
    if not streams:
        return
    chunk_size = max(1, int(getattr(manager._cfg, "subscribe_chunk_size", 100) or 100))
    delay_seconds = max(0.0, float(getattr(manager._cfg, "subscribe_chunk_delay_ms", 0)) / 1000.0)
    for offset in range(0, len(streams), chunk_size):
        if manager._ws_conns.get(endpoint) is None:
            break
        chunk = streams[offset : offset + chunk_size]
        message = json.dumps({"method": method, "params": chunk, "id": manager._subscribe_id})
        manager._subscribe_id += 1
        try:
            await ws_conn.send(message)
            LOG.debug(
                "ws %s chunk | endpoint=%s offset=%d streams=%d",
                method,
                endpoint,
                offset,
                len(chunk),
            )
        except (
            ws_exceptions.ConnectionClosed,
            ConnectionError,
            OSError,
            AttributeError,
        ) as exc:
            LOG.debug("ws %s failed (non-fatal) | endpoint=%s error=%s", method, endpoint, exc)
            break
        if offset + chunk_size < len(streams):
            await asyncio.sleep(delay_seconds)


async def resubscribe_all(manager: Any, endpoint: str, ws: Any) -> None:
    streams = sorted(manager._intended_streams_by_endpoint.get(endpoint, set()))
    if not streams:
        return
    if len(streams) > 200:
        LOG.info(
            "ws high stream count | endpoint=%s streams=%d symbols=%d - "
            "consider reducing shortlist_limit in config",
            endpoint,
            len(streams),
            len(manager._symbols),
        )
    previous_conn = manager._ws_conns.get(endpoint)
    manager._ws_conns[endpoint] = ws
    if endpoint == "market":
        manager._ws_conn = ws
    try:
        await send_subscription_command(manager, endpoint, "SUBSCRIBE", streams)
    finally:
        restored_conn = ws if manager._running else previous_conn
        manager._ws_conns[endpoint] = restored_conn
        if endpoint == "market":
            manager._ws_conn = restored_conn
    chunk_count = (
        len(streams) + manager._cfg.subscribe_chunk_size - 1
    ) // manager._cfg.subscribe_chunk_size
    LOG.info(
        "ws resubscribe sent | endpoint=%s streams=%d chunks=%d",
        endpoint,
        len(streams),
        chunk_count,
    )

# --- inlined from ws_connection.py ---
_WS_PING_INTERVAL_SECONDS = 20.0
_WS_PING_TIMEOUT_SECONDS = 60.0
_WS_CLOSE_TIMEOUT_SECONDS = 10.0
_WS_CONNECT_TIMEOUT_SECONDS = 60.0


def build_stream_url(manager: Any, endpoint: str) -> str:
    base = manager._cfg.endpoint_base_url(endpoint).rstrip("/")
    if base.endswith("/ws"):
        base = base.removesuffix("/ws")
    if base.endswith("/stream"):
        base = base.removesuffix("/stream")
    return f"{base}/stream"


def get_ws_fallback_urls(manager: Any, endpoint: str) -> list[str]:
    """Return endpoint-specific websocket URL candidates."""
    return [build_stream_url(manager, endpoint)]


def get_ws_url_version(manager: Any, endpoint: str) -> str:
    _ = manager
    if endpoint in {"public", "market"}:
        return endpoint
    return "unknown"


def apply_tcp_keepalive(_manager: Any, ws: Any) -> None:
    try:
        transport = getattr(ws, "transport", None)
        sock = transport.get_extra_info("socket") if transport is not None else None
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
        LOG.debug("tcp keepalive applied")
    except (OSError, AttributeError) as exc:
        LOG.debug("tcp keepalive not applied: %s", exc)


def apply_connected_state(manager: Any, *, endpoint: str, ws: Any, url: str) -> None:
    """Apply connection state updates after a successful websocket connect."""
    manager._ws_conns[endpoint] = ws
    manager._connected_urls[endpoint] = url
    manager._connected_at_by_endpoint[endpoint] = time.monotonic()
    if endpoint == "market":
        manager._ws_conn = ws

    apply_tcp_keepalive(manager, ws)

    manager._last_message_ts_by_endpoint[endpoint] = 0.0
    manager._last_message_ts = 0.0
    manager._last_event_lag_ms = None
    manager._connected_endpoints[endpoint].set()
    manager._refresh_connected_event()

    manager._connect_counts[endpoint] += 1
    manager._connect_count += 1
    if manager._connect_counts[endpoint] > 1 and manager._reconnect_cb is not None:
        task = asyncio.create_task(manager._reconnect_cb(), name=f"ws_reconnect_cb:{endpoint}")
        track = getattr(manager, "_track_callback_task", None)
        if callable(track):
            track(task, label=f"ws_reconnect:{endpoint}")

    LOG.info(
        "ws connected | endpoint=%s url=%s streams=%d connect_count=%d endpoint_connect_count=%d",
        endpoint,
        url,
        len(manager._intended_streams_by_endpoint.get(endpoint, set())),
        manager._connect_count,
        manager._connect_counts[endpoint],
    )
    manager._last_reconnect_reason = f"{endpoint}:connected"
    manager._last_reconnect_reason_by_endpoint[endpoint] = "connected"


def clear_endpoint_connection_state(manager: Any, endpoint: str) -> None:
    """Reset volatile state for a disconnected endpoint."""
    manager._ws_conns[endpoint] = None
    manager._connected_urls[endpoint] = None
    manager._connected_at_by_endpoint[endpoint] = 0.0
    manager._connected_endpoints[endpoint].clear()
    manager._refresh_connected_event()
    if endpoint == "market":
        manager._ws_conn = None


async def run_stream_session(
    manager: Any,
    *,
    endpoint: str,
    url: str,
    connect_start: float,
    backoff_reset_after_seconds: float,
    proactive_reconnect_after_seconds: float,
    parse_message: Any,
) -> tuple[bool, bool]:
    """Run one websocket session.

    Returns:
        Tuple (backoff_reset, proactive_reconnect_triggered).
    """
    proxy_url = getattr(manager, "_proxy_url", None)
    trust_env = bool(getattr(manager, "_trust_env", True))
    connect_kwargs: dict[str, Any] = websockets_connect_kwargs(
        proxy_url=proxy_url, trust_env=trust_env
    )
    ws = await asyncio.wait_for(
        websockets.connect(
            url,
            ping_interval=_WS_PING_INTERVAL_SECONDS,
            ping_timeout=_WS_PING_TIMEOUT_SECONDS,
            close_timeout=_WS_CLOSE_TIMEOUT_SECONDS,
            **connect_kwargs,
        ),
        timeout=_WS_CONNECT_TIMEOUT_SECONDS,
    )
    LOG.info("ws connection established | endpoint=%s url=%s", endpoint, url)
    backoff_reset = False
    reconnect_reason: str | None = None
    graceful_close = False
    async with ws:
        apply_connected_state(manager, endpoint=endpoint, ws=ws, url=url)
        await manager._resubscribe_all(endpoint, ws)
        stream_count = len(manager._intended_streams_by_endpoint.get(endpoint, set()))
        if stream_count > 120:
            LOG.info(
                "high stream count | endpoint=%s streams=%d shortlist=%d",
                endpoint,
                stream_count,
                len(manager._symbols),
            )
        health_task = asyncio.create_task(
            manager._health_monitor(ws, endpoint),
            name=f"ws_manager_health:{endpoint}",
        )
        try:
            async for raw in ws:
                if not manager._running:
                    reconnect_reason = "shutdown"
                    graceful_close = True
                    with contextlib.suppress(Exception):
                        await ws.close()
                    break
                elapsed = time.monotonic() - connect_start
                if not backoff_reset and elapsed >= backoff_reset_after_seconds:
                    backoff_reset = True
                    manager._short_lived_streak = 0
                if elapsed >= proactive_reconnect_after_seconds:
                    reconnect_reason = "24h_proactive"
                    break
                try:
                    msg = parse_message(raw)
                except DEFENSIVE_EXC as exc:
                    LOG.debug(
                        "websocket message parse failed | endpoint=%s error=%s", endpoint, exc
                    )
                    continue
                await manager._handle_message(msg, endpoint)
            else:
                close_code = getattr(ws, "close_code", None)
                if close_code in (1000, 1001):
                    graceful_close = True
        finally:
            health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await health_task

    clear_endpoint_connection_state(manager, endpoint)
    if reconnect_reason == "24h_proactive":
        manager._last_reconnect_reason = f"{endpoint}:{reconnect_reason}"
        manager._last_reconnect_reason_by_endpoint[endpoint] = reconnect_reason
        LOG.info(
            "ws proactive reconnect | endpoint=%s uptime=%.1fh",
            endpoint,
            (time.monotonic() - connect_start) / 3600,
        )
        return backoff_reset, True
    if reconnect_reason == "shutdown" or graceful_close:
        manager._last_reconnect_reason = f"{endpoint}:graceful_close"
        manager._last_reconnect_reason_by_endpoint[endpoint] = "graceful_close"
        LOG.info("ws graceful close | endpoint=%s", endpoint)
        return backoff_reset, False
    msg_0 = "stream closed without explicit close frame"
    raise ConnectionError(msg_0)


async def run_endpoint_loop(
    manager: Any,
    *,
    endpoint: str,
    backoff_reset_after_seconds: float,
    proactive_reconnect_after_seconds: float,
    parse_message: Any,
    market_endpoint: str,
    stale_symbols: Any,
    maybe_backfill_after_disconnect: Any,
    compute_disconnect_delay: Any,
    connection_exceptions: tuple[type[BaseException], ...],
) -> None:
    """Run reconnect loop for a websocket endpoint."""
    delay = 1.0
    retry_streak = 0
    max_delay = manager._cfg.reconnect_max_delay_seconds
    url = build_stream_url(manager, endpoint)
    while manager._running:
        connect_start = time.monotonic()
        backoff_reset = False
        try:
            LOG.info(
                "ws connecting | endpoint=%s url=%s streams=%d",
                endpoint,
                url,
                len(manager._intended_streams_by_endpoint.get(endpoint, set())),
            )
            backoff_reset, proactive_reconnect = await run_stream_session(
                manager,
                endpoint=endpoint,
                url=url,
                connect_start=connect_start,
                backoff_reset_after_seconds=backoff_reset_after_seconds,
                proactive_reconnect_after_seconds=proactive_reconnect_after_seconds,
                parse_message=parse_message,
            )
            retry_streak = 0
            if backoff_reset:
                delay = 1.0
            if proactive_reconnect:
                delay = 1.0
                manager._short_lived_streak = 0
                if endpoint == market_endpoint:
                    stale = stale_symbols()
                    await maybe_backfill_after_disconnect(
                        elapsed=time.monotonic() - connect_start,
                        stale_symbols=stale,
                    )
                continue
        except asyncio.CancelledError:
            clear_endpoint_connection_state(manager, endpoint)
            return
        except connection_exceptions as exc:
            clear_endpoint_connection_state(manager, endpoint)
            if not manager._running:
                return

            elapsed = time.monotonic() - connect_start
            retry_streak += 1
            if retry_streak <= 3:
                LOG.info("ws fast-retry %d/3 | endpoint=%s error=%s", retry_streak, endpoint, exc)
                delay = 0.5
            else:
                delay = compute_disconnect_delay(
                    manager,
                    endpoint=endpoint,
                    url=url,
                    exc=exc,
                    elapsed=elapsed,
                    delay=delay,
                )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if retry_streak > 3:
                delay = min(delay * 2.0, max_delay)
            if endpoint == market_endpoint:
                stale = stale_symbols()
                await maybe_backfill_after_disconnect(
                    elapsed=elapsed,
                    stale_symbols=stale,
                )
        except DEFENSIVE_EXC as exc:
            LOG.exception(
                "ws unexpected error during connection | endpoint=%s (%s)",
                endpoint,
                type(exc).__name__,
            )
            clear_endpoint_connection_state(manager, endpoint)
            if not manager._running:
                return
            retry_streak += 1
            delay = min(max(delay, 1.0) * 2.0, max_delay) if retry_streak > 3 else 0.5
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

# --- inlined from ws_cache.py ---
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
        if price <= 0.0 or qty <= 0.0:
            continue
        parsed.append((price, qty))
    parsed.sort(key=lambda item: item[0], reverse=reverse)
    return tuple(parsed)


def _l2_depth_imbalance(manager: Any, symbol: str) -> float | None:
    book = manager._depth_book.get(symbol)
    if not book:
        return None
    bids = book.get("bids") or ()
    asks = book.get("asks") or ()
    if not bids or not asks:
        return None
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0.0:
        return None
    band_bps = float(getattr(manager._cfg, "depth_band_bps", 8.0) or 8.0)
    band = mid * band_bps / 10_000.0

    bid_notional = sum(price * qty for price, qty in bids if (mid - price) <= band)
    ask_notional = sum(price * qty for price, qty in asks if (price - mid) <= band)
    if bid_notional <= 0.0 and ask_notional <= 0.0:
        bid_notional = sum(price * qty for price, qty in bids)
        ask_notional = sum(price * qty for price, qty in asks)
    total = bid_notional + ask_notional
    if total <= 0.0:
        return None
    imbalance = (bid_notional - ask_notional) / total
    wall_pressure = manager._depth_wall_pressure.get(symbol)
    if wall_pressure is not None:
        imbalance = (imbalance * 0.65) + (float(wall_pressure) * 0.35)
    return float(round(max(-1.0, min(1.0, imbalance)), 4))


def _update_depth_wall_pressure(
    manager: Any,
    symbol: str,
    bids: tuple[tuple[float, float], ...],
    asks: tuple[tuple[float, float], ...],
    now: float,
) -> None:
    min_notional = float(getattr(manager._cfg, "depth_wall_min_notional", 250_000.0) or 0.0)
    if min_notional <= 0.0:
        manager._depth_wall_pressure.pop(symbol, None)
        return
    persistence = float(getattr(manager._cfg, "depth_wall_persistence_seconds", 10.0) or 0.0)
    stale_after = max(5.0, persistence * 2.0)
    state = manager._depth_wall_state.setdefault(symbol, {})
    seen: set[tuple[str, float]] = set()
    bid_wall = ask_wall = 0.0

    for side, levels in (("bid", bids), ("ask", asks)):
        for price, qty in levels:
            notional = price * qty
            if notional < min_notional:
                continue
            key = (side, round(price, 8))
            seen.add(key)
            item = state.get(key)
            if item is None:
                item = {"first_seen": now, "last_seen": now, "max_notional": notional}
                state[key] = item
            else:
                item["last_seen"] = now
                item["max_notional"] = max(float(item.get("max_notional", 0.0)), notional)
            if now - float(item.get("first_seen", now)) >= persistence:
                if side == "bid":
                    bid_wall += notional
                else:
                    ask_wall += notional

    for key, item in list(state.items()):
        if key not in seen and now - float(item.get("last_seen", 0.0)) > stale_after:
            state.pop(key, None)

    total = bid_wall + ask_wall
    if total > 0.0:
        manager._depth_wall_pressure[symbol] = round(
            max(-1.0, min(1.0, (bid_wall - ask_wall) / total)), 4
        )
    else:
        manager._depth_wall_pressure.pop(symbol, None)


def is_ticker_cache_warm(manager: Any) -> bool:
    if not manager._ticker_cache:
        return False
    age = time.monotonic() - manager._ticker_cache_ts
    return bool(age <= manager._cfg.market_ticker_freshness_seconds)


def get_stats(manager: Any) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "streams_total": len(manager._intended_streams),
        "streams_active": len(manager._stream_last_message_ts),
        "slow_streams": list(manager._slow_streams),
        "slow_streams_count": len(manager._slow_streams),
        "buffer_stats": manager._message_buffer.get_stats(),
    }
    stream_latencies: dict[str, float] = {}
    for stream, latencies in manager._stream_latency_ms.items():
        if latencies:
            stream_latencies[stream] = round(sum(latencies) / len(latencies), 2)
    if stream_latencies:
        stats["avg_latency_per_stream"] = stream_latencies
        all_latencies = [sum(v) / len(v) for v in manager._stream_latency_ms.values() if v]
        if all_latencies:
            stats["avg_latency_overall_ms"] = round(sum(all_latencies) / len(all_latencies), 2)
    return stats


def get_global_ticker_data(manager: Any) -> list[JsonDict]:
    result: list[JsonDict] = []
    now = time.monotonic()
    for symbol, ticker in manager._ticker_cache.items():
        last_update = manager._ticker_update_times.get(symbol, 0.0)
        if now - last_update > manager._cfg.market_ticker_freshness_seconds:
            continue
        result.append(
            {
                "symbol": symbol,
                "quote_volume": ticker.get("quote_volume", 0.0),
                "price_change_percent": ticker.get("price_change_percent", 0.0),
                "last_price": ticker.get("last_price", 0.0),
                "trade_count": int(float(ticker.get("trade_count") or 0)),
            }
        )
    return result


def get_depth_imbalance(manager: Any, symbol: str) -> float | None:
    l2_imbalance = _l2_depth_imbalance(manager, symbol)
    if l2_imbalance is not None:
        return l2_imbalance
    bid_qty, ask_qty = manager._book_qty.get(symbol, (None, None))
    snapshot = manager.get_agg_trade_snapshot(symbol)
    return depth_imbalance_from_book(
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        delta_ratio=None if snapshot is None else snapshot.delta_ratio,
    )


def get_depth_imbalance_source(manager: Any, symbol: str) -> str | None:
    if _l2_depth_imbalance(manager, symbol) is not None:
        return "l2_depth"
    bid_qty, ask_qty = manager._book_qty.get(symbol, (None, None))
    if bid_qty is not None and ask_qty is not None and bid_qty >= 0 and ask_qty >= 0:
        return "l1_book"
    if manager.get_agg_trade_snapshot(symbol) is not None:
        return "agg_trade_proxy"
    return None


def get_microprice_bias(manager: Any, symbol: str) -> float | None:
    bid, ask = manager.get_book_snapshot(symbol)
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    bid_qty, ask_qty = manager._book_qty.get(symbol, (None, None))
    snapshot = manager.get_agg_trade_snapshot(symbol)
    return microprice_bias_from_book(
        bid=bid,
        ask=ask,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        delta_ratio=None if snapshot is None else snapshot.delta_ratio,
    )


def get_microprice_bias_source(manager: Any, symbol: str) -> str | None:
    bid, ask = manager.get_book_snapshot(symbol)
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    if manager._depth_book.get(symbol):
        return "l2_depth"
    bid_qty, ask_qty = manager._book_qty.get(symbol, (None, None))
    if bid_qty is not None and ask_qty is not None and bid_qty >= 0 and ask_qty >= 0:
        return "l1_book"
    if manager.get_agg_trade_snapshot(symbol) is not None:
        return "agg_trade_proxy"
    return None


def get_funding_sentiment(manager: Any) -> float | None:
    rates: list[float] = []
    for value in manager._mark_price_cache.values():
        raw_rate = value.get("funding_rate")
        if raw_rate is None:
            continue
        try:
            rates.append(float(raw_rate))
        except (TypeError, ValueError):
            continue
    if not rates:
        return None
    return sum(rates) / len(rates)


def get_liquidation_rollups(
    manager: Any,
    symbol: str | None = None,
    window_seconds: int = 900,
) -> dict[str, float] | None:
    """Return notional-weighted liquidation rollups for *symbol* (ORDER_FLOW_INGEST §3)."""
    cutoff_ms = int(time.time() * 1000) - window_seconds * 1000
    long_notional = 0.0
    short_notional = 0.0
    for ts_ms, sym, side, qty, price in manager._force_order_buffer:
        if ts_ms < cutoff_ms:
            continue
        if symbol is not None and sym != symbol:
            continue
        try:
            qty_val = float(qty)
            price_val = float(price)
        except (TypeError, ValueError):
            continue
        if qty_val <= 0.0:
            continue
        notional = qty_val * price_val if price_val > 0.0 else qty_val
        if side == "BUY":
            short_notional += notional
        else:
            long_notional += notional
    total = long_notional + short_notional
    if total <= 0.0:
        return None
    return {
        "liquidation_long_notional": long_notional,
        "liquidation_short_notional": short_notional,
        "liquidation_total_notional": total,
        "liquidation_score": (short_notional - long_notional) / total,
    }


def get_liquidation_sentiment(
    manager: Any,
    symbol: str | None = None,
    window_seconds: int = 60,
) -> float | None:
    rollups = get_liquidation_rollups(manager, symbol=symbol, window_seconds=window_seconds)
    if rollups is None:
        return None
    return float(rollups["liquidation_score"])


def get_liquidation_age_seconds(
    manager: Any,
    symbol: str | None = None,
    window_seconds: int = 60,
) -> float | None:
    cutoff_ms = int(time.time() * 1000) - window_seconds * 1000
    latest_ts_ms: int | None = None
    for ts_ms, sym, _side, qty, _price in manager._force_order_buffer:
        if ts_ms < cutoff_ms or qty <= 0.0:
            continue
        if symbol is not None and sym != symbol:
            continue
        if latest_ts_ms is None or ts_ms > latest_ts_ms:
            latest_ts_ms = ts_ms
    if latest_ts_ms is None:
        return None
    return max(0.0, (int(time.time() * 1000) - latest_ts_ms) / 1000.0)


def should_throttle_ticker_update(manager: Any, symbol: str) -> bool:
    now = time.monotonic()
    last_update = manager._ticker_update_times.get(symbol, 0.0)
    elapsed_ms = (now - last_update) * 1000
    if elapsed_ms < manager._min_ticker_update_interval_ms:
        last_logged = getattr(manager, "_last_ticker_throttle_log", {}).get(symbol, 0.0)
        if now - last_logged >= 30.0:
            if not hasattr(manager, "_last_ticker_throttle_log"):
                manager._last_ticker_throttle_log = {}
            manager._last_ticker_throttle_log[symbol] = now
            LOG.debug(
                "ticker throttled | symbol=%s elapsed=%.0fms min=%.0fms",
                symbol,
                elapsed_ms,
                manager._min_ticker_update_interval_ms,
            )
        return True
    manager._ticker_update_times[symbol] = now
    return False


def should_throttle_mark_price_update(manager: Any, symbol: str) -> bool:
    now = time.monotonic()
    last_update = manager._mark_price_update_times.get(symbol, 0.0)
    elapsed_ms = (now - last_update) * 1000
    if elapsed_ms < 50.0:
        last_logged = getattr(manager, "_last_markprice_throttle_log", {}).get(symbol, 0.0)
        if now - last_logged >= 30.0:
            if not hasattr(manager, "_last_markprice_throttle_log"):
                manager._last_markprice_throttle_log = {}
            manager._last_markprice_throttle_log[symbol] = now
            LOG.debug(
                "mark_price throttled | symbol=%s elapsed=%.0fms min=50ms",
                symbol,
                elapsed_ms,
            )
        return True
    manager._mark_price_update_times[symbol] = now
    return False


def handle_ticker(manager: Any, symbol: str, data: JsonDict) -> None:
    if should_throttle_ticker_update(manager, symbol):
        return
    try:
        manager._ticker_cache[symbol] = {
            "symbol": symbol,
            "last_price": float(data.get("c") or 0.0),
            "quote_volume": float(data.get("q") or 0.0),
            "price_change_percent": float(data.get("P") or 0.0),
            "price_change": float(data.get("p") or 0.0),
            "open_price": float(data.get("o") or 0.0),
            "high_price": float(data.get("h") or 0.0),
            "low_price": float(data.get("l") or 0.0),
            "trade_count": int(float(data.get("n") or 0)),
        }
        manager._ticker_cache_ts = time.monotonic()
    except (TypeError, ValueError):
        return


def handle_mini_ticker(manager: Any, symbol: str, data: JsonDict) -> None:
    now = time.monotonic()
    last_full_update = manager._ticker_update_times.get(symbol, 0.0)
    if now - last_full_update < manager._cfg.market_ticker_freshness_seconds:
        return
    if should_throttle_ticker_update(manager, symbol):
        return
    try:
        close_price = float(data.get("c") or 0.0)
        open_price = float(data.get("o") or 0.0)
        price_change_pct = (
            ((close_price - open_price) / open_price * 100.0) if open_price > 0 else 0.0
        )
        manager._ticker_cache[symbol] = {
            "symbol": symbol,
            "last_price": close_price,
            "quote_volume": float(data.get("q") or 0.0),
            "price_change_percent": price_change_pct,
            "price_change": close_price - open_price,
            "open_price": open_price,
            "high_price": float(data.get("h") or 0.0),
            "low_price": float(data.get("l") or 0.0),
        }
        manager._ticker_cache_ts = time.monotonic()
    except (TypeError, ValueError):
        return


def handle_mark_price(manager: Any, symbol: str, data: JsonDict) -> None:
    if not symbol:
        return
    if should_throttle_mark_price_update(manager, symbol):
        return
    try:
        funding_str = data.get("r")
        # Ensure funding_str is not None for float() or check specifically
        funding_rate = (
            float(funding_str) if funding_str is not None and funding_str not in ("", "0") else 0.0
        )
        mark_price = float(data.get("p") or 0.0)
        if mark_price <= 0.0:
            return
        now = time.monotonic()
        manager._mark_price_cache[symbol] = {
            "symbol": symbol,
            "mark_price": mark_price,
            "index_price": float(data.get("i") or 0.0),
            "funding_rate": funding_rate,
            "next_funding_time_ms": int(data.get("T") or 0),
            "updated_at": now,
        }
        manager._mark_price_update_times[symbol] = now
    except (TypeError, ValueError):
        return


def handle_force_order(manager: Any, data: JsonDict) -> None:
    try:
        order = data.get("o", {})
        symbol = str(order.get("s") or "").upper()
        side = str(order.get("S") or "").upper()
        qty = float(order.get("q") or 0.0)
        try:
            price = float(order.get("p", 0) or 0)
        except (TypeError, ValueError):
            price = 0.0
        ts_ms = int(order.get("T") or data.get("E") or (time.time() * 1000))
        if symbol and side in ("BUY", "SELL") and qty > 0:
            manager._force_order_buffer.append((ts_ms, symbol, side, qty, price))
    except (TypeError, ValueError, KeyError):
        return


async def handle_book_ticker(manager: Any, symbol: str, data: JsonDict) -> None:
    if manager._symbols and symbol not in manager._symbols:
        return
    try:
        bid = float(data["b"]) if data.get("b") is not None else None
        ask = float(data["a"]) if data.get("a") is not None else None
        bid_qty = float(data["B"]) if data.get("B") is not None else None
        ask_qty = float(data["A"]) if data.get("A") is not None else None
        event_ts_ms = int(data["E"]) if data.get("E") is not None else None
    except (KeyError, TypeError, ValueError):
        return

    async with manager._data_lock:
        manager._book[symbol] = (bid, ask)
        manager._book_qty[symbol] = (bid_qty, ask_qty)
        manager._book_update_times[symbol] = time.monotonic()

    if manager._event_bus is not None:
        manager._event_bus.publish_nowait(
            BookTickerEvent(symbol=symbol, bid=bid, ask=ask, event_ts_ms=event_ts_ms)
        )


async def handle_depth_update(manager: Any, symbol: str, data: JsonDict) -> None:
    if manager._symbols and symbol not in manager._symbols:
        return
    bids = _parse_depth_levels(data.get("b"), reverse=True)
    asks = _parse_depth_levels(data.get("a"), reverse=False)
    if not bids or not asks:
        return

    now = time.monotonic()
    async with manager._data_lock:
        manager._depth_book[symbol] = {"bids": bids, "asks": asks}
        manager._depth_update_times[symbol] = now
        manager._book[symbol] = (bids[0][0], asks[0][0])
        manager._book_qty[symbol] = (bids[0][1], asks[0][1])
        manager._book_update_times[symbol] = now
        _update_depth_wall_pressure(manager, symbol, bids, asks, now)


async def handle_agg_trade(manager: Any, symbol: str, data: JsonDict) -> None:
    if manager._symbols and symbol not in manager._symbols:
        return
    try:
        trade = AggTrade(
            symbol=symbol,
            trade_id=int(data["a"]),
            price=float(data["p"]),
            quantity=float(data["q"]),
            trade_time_ms=int(data["T"]),
            is_buyer_maker=bool(data["m"]),
        )
    except (KeyError, TypeError, ValueError):
        return

    pending = manager._pending_agg_trades.setdefault(symbol, [])
    pending.append(trade)
    now = time.monotonic()
    last_flush = manager._last_agg_trade_flush_ts.get(symbol, 0.0)
    flush_interval = float(manager._cfg.agg_trade_flush_interval_ms) / 1000.0
    if now - last_flush < flush_interval and len(pending) < 500:
        return

    batch = pending[:]
    pending.clear()
    manager._last_agg_trade_flush_ts[symbol] = now

    if symbol not in manager._agg_trades:
        manager._agg_trades[symbol] = collections.deque(maxlen=manager._cfg.max_agg_trade_buffer)
    manager._agg_trades[symbol].extend(batch)

    if manager._agg_trade_cbs:
        trade_dt = datetime.fromtimestamp(batch[-1].trade_time_ms / 1000.0, tz=UTC)
        for callback in manager._agg_trade_cbs:
            task = asyncio.create_task(callback(symbol, batch[-1].price, trade_dt))
            manager._attach_task_logging(task, label=f"agg_trade:{symbol}")

class FuturesWSManager:
    """Manages WebSocket connections to Binance Futures for real-time market data."""

    def __init__(
        self,
        rest_client: BinanceFuturesMarketData,
        config: WSConfig,
        *,
        proxy_url: str | None = None,
        trust_env: bool = True,
    ) -> None:
        self._rest = rest_client
        self._cfg = config
        inner = getattr(rest_client, "_binance_client", None)
        self._proxy_url = proxy_url or getattr(inner, "_proxy_url", None)
        self._trust_env = trust_env if proxy_url else getattr(inner, "_trust_env", trust_env)
        self._symbols: list[str] = []
        self._tracked_symbols: list[str] = []
        self._lock = asyncio.Lock()
        self._data_lock = asyncio.Lock()
        self._backfill_sem = asyncio.Semaphore(5)

        self._klines: dict[str, dict[str, collections.deque[JsonDict]]] = {}
        self._book: dict[str, tuple[float | None, float | None]] = {}
        self._book_qty: dict[str, tuple[float | None, float | None]] = {}
        self._depth_book: dict[str, dict[str, tuple[tuple[float, float], ...]]] = {}
        self._depth_update_times: dict[str, float] = {}
        self._depth_wall_state: dict[str, dict[tuple[str, float], dict[str, float]]] = {}
        self._depth_wall_pressure: dict[str, float] = {}
        self._agg_trades: dict[str, collections.deque[AggTrade]] = {}
        self._pending_agg_trades: dict[str, list[AggTrade]] = {}
        self._last_agg_trade_flush_ts: dict[str, float] = {}
        self._max_streams_per_connection = _MAX_STREAMS_PER_CONNECTION

        # Global market stream caches (populated from !ticker@arr, !markPrice@arr, !forceOrder@arr)
        self._ticker_cache: dict[str, JsonDict] = {}
        self._ticker_cache_ts: float = 0.0  # monotonic time of last ticker update
        self._mark_price_cache: dict[str, JsonDict] = {}
        # Force order buffer: (timestamp_ms, symbol, side, qty, price)
        self._force_order_buffer: collections.deque[tuple[int, str, str, float, float]] = (
            collections.deque(maxlen=500)
        )

        self._stream_task: asyncio.Task[None] | None = None
        self._stream_tasks: dict[str, asyncio.Task[None] | None] = dict.fromkeys(_WS_ENDPOINTS)
        self._running = False
        self._connected = asyncio.Event()
        self._connected_endpoints: dict[str, asyncio.Event] = {
            endpoint: asyncio.Event() for endpoint in _WS_ENDPOINTS
        }
        self._ws_conn: Any | None = None
        self._ws_conns: dict[str, Any | None] = dict.fromkeys(_WS_ENDPOINTS)
        self._subscribe_id = 1
        self._intended_streams: set[str] = set()
        self._intended_streams_by_endpoint: dict[str, set[str]] = {
            endpoint: set() for endpoint in _WS_ENDPOINTS
        }
        self._last_message_ts = 0.0
        self._last_message_ts_by_endpoint: dict[str, float] = dict.fromkeys(_WS_ENDPOINTS, 0.0)
        self._last_event_lag_ms: float | None = None
        self._short_lived_streak = 0
        self._last_reconnect_reason = "not_started"
        self._last_reconnect_reason_by_endpoint: dict[str, str] = dict.fromkeys(
            _WS_ENDPOINTS, "not_started"
        )
        self._connected_urls: dict[str, str | None] = dict.fromkeys(_WS_ENDPOINTS)
        self._connected_at_by_endpoint: dict[str, float] = dict.fromkeys(_WS_ENDPOINTS, 0.0)
        self._subscription_errors: dict[str, Any | None] = dict.fromkeys(_WS_ENDPOINTS)
        self._subscription_ack_count: dict[str, int] = dict.fromkeys(_WS_ENDPOINTS, 0)
        self._backfill_cooldowns: dict[str, float] = {}
        self._last_latency_warning_by_symbol: dict[str, float] = {}
        self._last_stale_warning_by_stream: dict[str, float] = {}
        self._last_short_disconnect_s: float | None = None
        # Debounce and throttling state for global streams
        self._ticker_update_times: dict[str, float] = {}  # symbol -> last update monotonic
        self._mark_price_update_times: dict[str, float] = {}  # symbol -> last update monotonic
        self._book_update_times: dict[str, float] = {}
        self._min_ticker_update_interval_ms: float = 100.0  # throttle duplicate updates
        self._shortlist_rebuild_lock = asyncio.Lock()
        self._last_shortlist_rebuild_ts: float = 0.0
        self._shortlist_rebuild_interval_seconds: float = 75.0  # 60-90s as requested
        self._last_shortlist: list[Any] = []
        self._last_shortlist_summary: dict[str, Any] = {}

        # Event-driven callbacks (fire-and-forget via asyncio.create_task)
        self._kline_close_cbs: dict[
            str, list[KlineCloseCallback]
        ] = {}  # interval -> [async cb(symbol, interval, close_ts_ms)]
        self._agg_trade_cbs: list[AggTradeCallback] = []  # [async cb(symbol, price, ts)]
        self._reconnect_cb: ReconnectCallback | None = None  # fired on reconnect

        # Message buffering with background draining.
        self._message_buffer = MessageBuffer(maxsize=_resolve_message_buffer_maxsize(config))
        self._rate_limiter = RateLimiter(
            _MAX_INCOMING_MSG_PER_SECOND
        )  # msg/sec: Binance WS inbound processing ceiling.
        self._buffer_processor_task: asyncio.Task[None] | None = None
        self._backfill_tasks: set[asyncio.Task[None]] = set()
        self._callback_tasks: set[asyncio.Task[Any]] = set()
        self._backfill_symbols_inflight: set[str] = set()
        self._stale_event_drop_count: int = 0
        self._epoch_monotonic_offset_ms = (time.time() - time.monotonic()) * 1000.0

        # P3: Per-stream latency monitoring
        self._stream_latency_ms: dict[
            str, collections.deque[float]
        ] = {}  # stream -> deque of last 10 latencies
        self._slow_streams: set[str] = set()  # streams with avg latency > 5000ms
        self._stream_last_message_ts: dict[str, float] = {}  # last message timestamp per stream
        self._connect_count: int = 0  # incremented each successful connection
        self._connect_counts: dict[str, int] = dict.fromkeys(_WS_ENDPOINTS, 0)

        # EventBus integration (optional — set via set_event_bus())
        self._event_bus: EventBus | None = None
        self._event_bus_missing_logged = False
        self._radar_store: Any | None = None

    @staticmethod
    def _normalize_symbol_list(items: list[Any]) -> list[str]:
        normalized: list[str] = []
        for item in items or []:
            sym: Any = item
            if not isinstance(sym, str) and hasattr(sym, "symbol"):
                try:
                    sym = sym.symbol
                except DEFENSIVE_EXC as exc:
                    LOG.debug("symbol normalization fallback failed: %s", exc)
                    sym = item
            sym = str(sym).strip().upper()
            if not sym:
                continue
            normalized.append(sym)
        # preserve order while dropping duplicates
        return list(dict.fromkeys(normalized))

    def _should_subscribe_agg_trade(self) -> bool:
        return bool(self._cfg.subscribe_agg_trade)

    # ------------------------------------------------------------------
    # Event-driven callback registration
    # ------------------------------------------------------------------

    def register_kline_close(self, interval: str, cb: KlineCloseCallback) -> None:
        """Register an async callback fired when a kline of *interval* closes.

        Callback signature: ``async def cb(symbol: str, interval: str, close_ts_ms: int) -> None``
        """
        self._kline_close_cbs.setdefault(interval, []).append(cb)

    def register_agg_trade(self, cb: AggTradeCallback) -> None:
        """Register an async callback fired on every aggTrade tick.

        Callback signature: ``async def cb(symbol: str, price: float, ts: datetime) -> None``
        """
        self._agg_trade_cbs.append(cb)

    def register_reconnect(self, cb: ReconnectCallback) -> None:
        """Register an async callback fired when the WS reconnects (not on first connect).

        Callback signature: ``async def cb() -> None``
        """
        self._reconnect_cb = cb

    def set_event_bus(self, bus: EventBus) -> None:
        """Attach an EventBus.  When set, kline_close events are published to it
        in addition to (or instead of) the legacy callback system.

        Called by SignalBot.__init__ before ws_manager.start().
        """
        self._event_bus = bus
        LOG.info("EventBus attached to ws_manager")

    def set_radar_store(self, store: Any) -> None:
        """Attach radar persistence for subscription rebuild after reconnect."""
        self._radar_store = store

    @staticmethod
    def _attach_task_logging(task: asyncio.Task[Any], *, label: str) -> None:
        def _done(done: asyncio.Task[Any]) -> None:
            with contextlib.suppress(asyncio.CancelledError):
                exc = done.exception()
                if exc is not None:
                    LOG.exception("%s callback failed", label, exc_info=exc)

        task.add_done_callback(_done)

    def _track_callback_task(self, task: asyncio.Task[Any], *, label: str) -> None:
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)
        self._attach_task_logging(task, label=label)

    def _schedule_backfill(
        self,
        symbols: list[str],
        *,
        name: str,
    ) -> asyncio.Task[None] | None:
        scheduled_symbols = [
            symbol
            for symbol in self._normalize_symbol_list(symbols)
            if symbol not in self._backfill_symbols_inflight
        ]
        if not scheduled_symbols:
            return None

        self._backfill_symbols_inflight.update(scheduled_symbols)

        async def _run_backfill() -> None:
            try:
                await self._backfill(scheduled_symbols)
            finally:
                self._backfill_symbols_inflight.difference_update(scheduled_symbols)

        task = asyncio.create_task(_run_backfill(), name=name)
        self._backfill_tasks.add(task)
        task.add_done_callback(self._backfill_tasks.discard)
        self._attach_task_logging(task, label=name)
        return task

    async def start(self, symbols: list[str]) -> None:
        """Start the WebSocket manager with the given symbols.

        Args:
            symbols: List of trading symbols to subscribe to.
        """
        if self._running:
            LOG.debug("ws_manager already running, ignoring start() call")
            return
        async with self._lock:
            self._symbols = self._normalize_symbol_list(list(symbols))
            self._recompute_intended_streams()

        # Start WS first; do not block startup on full REST backfill.
        # Backfill is scheduled in the background to keep the bot responsive.
        self._running = True
        for endpoint in self._active_endpoint_classes():
            self._stream_tasks[endpoint] = asyncio.create_task(
                self._run_stream(endpoint), name=f"ws_manager_stream:{endpoint}"
            )
        self._stream_task = self._stream_tasks.get(_WS_MARKET) or self._stream_tasks.get(_WS_PUBLIC)
        # P1: Start message buffer processor
        self._buffer_processor_task = asyncio.create_task(
            self._process_buffered_messages(), name="ws_buffer_processor"
        )
        LOG.info(
            "ws_manager started | symbols=%d endpoints=%s",
            len(self._symbols),
            list(self._active_endpoint_classes()),
        )

        if self._symbols:
            self._schedule_backfill(self._symbols, name="ws_backfill_initial")

    def update_proxy_url(self, proxy_url: str | None, *, trust_env: bool | None = None) -> None:
        """Switch egress proxy and force WS reconnect when already running."""
        self._proxy_url = normalize_proxy_url(proxy_url) if proxy_url else None
        if trust_env is not None:
            self._trust_env = trust_env
        if self._proxy_url:
            apply_proxy_env(self._proxy_url)
        LOG.info("ws proxy updated | url=%s", mask_proxy_url(self._proxy_url or ""))
        if self._running:
            task = asyncio.create_task(self._close_connections_for_proxy_failover())
            self._backfill_tasks.add(task)
            task.add_done_callback(self._backfill_tasks.discard)

    async def _close_connections_for_proxy_failover(self) -> None:
        for endpoint, ws in list(self._ws_conns.items()):
            if ws is None:
                continue
            with contextlib.suppress(Exception):
                await ws.close()
            self._last_reconnect_reason_by_endpoint[endpoint] = "proxy_failover"
        self._last_reconnect_reason = "proxy_failover"

    async def close(self) -> None:
        """Alias for ``stop()`` (market data facade compatibility)."""
        await self.stop()

    async def preflight_check(self) -> None:
        """No-op preflight; connectivity is validated on ``start()``."""
        return

    async def stop(self) -> None:
        """Stop the WebSocket manager and close all connections."""
        if not self._running:
            return  # Already stopped or never started
        self._running = False
        self._connected.clear()
        self._ws_conn = None
        for endpoint in _WS_ENDPOINTS:
            self._connected_endpoints[endpoint].clear()
            self._ws_conns[endpoint] = None
            self._connected_urls[endpoint] = None
            self._connected_at_by_endpoint[endpoint] = 0.0
        tasks_to_cancel = [
            task for task in self._stream_tasks.values() if task is not None and not task.done()
        ]
        for task in tasks_to_cancel:
            task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        self._stream_tasks = dict.fromkeys(_WS_ENDPOINTS)
        self._stream_task = None

        # P1: Stop buffer processor
        if self._buffer_processor_task and not self._buffer_processor_task.done():
            self._buffer_processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._buffer_processor_task
        self._buffer_processor_task = None

        # Cancel any in-flight REST backfills (non-fatal, best-effort).
        if self._backfill_tasks:
            for task in list(self._backfill_tasks):
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*self._backfill_tasks, return_exceptions=True)
            self._backfill_tasks.clear()

        if self._callback_tasks:
            for task in list(self._callback_tasks):
                task.cancel()
            await asyncio.gather(*self._callback_tasks, return_exceptions=True)
            self._callback_tasks.clear()

        LOG.info("ws_manager stopped")

    async def wait_until_connected(self, *, max_wait_s: float | None = None) -> bool:
        """Wait until the WebSocket connection is established.

        Args:
            max_wait_s: Maximum time to wait in seconds. None means wait forever.

        Returns:
            True if connected, False if timeout occurred.
        """
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=max_wait_s)
        except TimeoutError:
            return False
        return True

    @property
    def is_running(self) -> bool:
        """Check if the WebSocket manager is currently running."""
        return self._running

    def is_connected(self) -> bool:
        """Check if WebSocket is connected and ready."""
        return self._connected.is_set()

    def _active_endpoint_classes(self) -> tuple[str, ...]:
        return tuple(
            endpoint
            for endpoint in _WS_ENDPOINTS
            if self._intended_streams_by_endpoint.get(endpoint)
        )

    def _refresh_connected_event(self) -> None:
        active = self._active_endpoint_classes()
        if active and all(self._connected_endpoints[endpoint].is_set() for endpoint in active):
            self._connected.set()
            return
        if not active:
            self._connected.clear()
            return
        self._connected.clear()

    def state_snapshot(self) -> dict[str, Any]:
        """Return a snapshot of the current WebSocket state.

        Returns:
            Dictionary with stream counts, cache health metrics, and reconnect info.
            Includes detailed cache metrics: ticker freshness, liquidation buffer size,
            and per-symbol freshness statistics.
        """
        warm = sum(1 for s in self._symbols if self.is_warm(s))
        total = len(self._symbols)
        now = time.monotonic()
        ticker_age = round(now - self._ticker_cache_ts, 1) if self._ticker_cache_ts > 0 else None
        fresh_tickers = sum(
            1
            for sym, ts in self._ticker_update_times.items()
            if now - ts <= self._cfg.market_ticker_freshness_seconds
        )
        freshness = self._cfg.market_ticker_freshness_seconds
        fresh_mark_prices = sum(
            1 for sym, ts in self._mark_price_update_times.items() if sym and now - ts <= freshness
        )
        if fresh_mark_prices <= 0:
            fresh_mark_prices = sum(
                1
                for sym, row in self._mark_price_cache.items()
                if sym
                and float(row.get("mark_price") or 0.0) > 0.0
                and now - float(row.get("updated_at") or 0.0) <= freshness
            )
        fresh_book_tickers = sum(
            1
            for sym, ts in self._book_update_times.items()
            if sym in self._symbols and now - ts <= self._cfg.market_ticker_freshness_seconds
        )
        fresh_depth_books = sum(
            1
            for sym, ts in self._depth_update_times.items()
            if sym in self._symbols and now - ts <= self._cfg.market_ticker_freshness_seconds
        )
        fresh_klines_15m = sum(1 for sym in self._symbols if self._is_interval_fresh(sym, "15m"))
        connected_urls = {
            endpoint: url for endpoint, url in self._connected_urls.items() if url is not None
        }
        connected_endpoints = [
            endpoint for endpoint in _WS_ENDPOINTS if self._connected_endpoints[endpoint].is_set()
        ]
        budget = getattr(self, "_subscription_budget", None)
        agg_symbols: set[str] = set()
        if budget is not None:
            agg_symbols = {str(sym).strip().lower() for sym in budget.agg_trade_symbols if sym}
        from .subscription_planner import ORDER_FLOW_ANCHOR_SYMBOLS

        anchor_symbols_in_agg_trade = sum(
            1 for anchor in ORDER_FLOW_ANCHOR_SYMBOLS if anchor in agg_symbols
        )
        return {
            "active_stream_count": len(self._intended_streams),
            "intended_stream_count": len(self._intended_streams),
            "warm_symbols": warm,
            "total_symbols": total,
            "reconnect_count": self._connect_count,
            "last_event_lag_ms": self._last_event_lag_ms,
            "avg_latency_ms": self._get_current_latency_ms(),
            "last_message_age_seconds": self._last_message_age_seconds(),
            "public_last_message_age_seconds": self._last_message_age_seconds(_WS_PUBLIC),
            "market_last_message_age_seconds": self._last_message_age_seconds(_WS_MARKET),
            "ticker_cache_age_seconds": ticker_age,
            "buffer_message_count": self._message_buffer.get_stats()["size"],
            "message_buffer": self._message_buffer.get_stats(),
            "stale_event_drop_count": self._stale_event_drop_count,
            "fresh_tickers": fresh_tickers,
            "fresh_mark_prices": fresh_mark_prices,
            "fresh_book_tickers": fresh_book_tickers,
            "fresh_depth_books": fresh_depth_books,
            "fresh_klines_15m": fresh_klines_15m,
            "public_connect_count": self._connect_counts[_WS_PUBLIC],
            "market_connect_count": self._connect_counts[_WS_MARKET],
            "mark_price_fresh_symbols": fresh_mark_prices,
            "liq_buffer_size": len(self._force_order_buffer),
            "stale_kline_stream_count": len(self._stale_kline_streams()),
            "connected_ws_url": connected_urls or None,
            "ws_endpoint_class": connected_endpoints or None,
            "public_subscription_ack_count": self._subscription_ack_count[_WS_PUBLIC],
            "market_subscription_ack_count": self._subscription_ack_count[_WS_MARKET],
            "public_subscription_error": self._subscription_errors[_WS_PUBLIC],
            "market_subscription_error": self._subscription_errors[_WS_MARKET],
            "order_flow_tracked_count": len(self._tracked_symbols),
            "anchor_symbols_in_agg_trade": anchor_symbols_in_agg_trade,
            # Shortlist rebuild state
            "last_shortlist_rebuild_age_s": round(now - self._last_shortlist_rebuild_ts, 1)
            if self._last_shortlist_rebuild_ts > 0
            else None,
        }

    def _get_current_latency_ms(self) -> float | None:
        """Calculate current WebSocket latency in milliseconds."""
        if not self._stream_latency_ms:
            return self._last_event_lag_ms
        all_latencies = [
            sum(latencies) / len(latencies)
            for latencies in self._stream_latency_ms.values()
            if latencies
        ]
        if all_latencies:
            return float(round(sum(all_latencies) / len(all_latencies), 2))
        return self._last_event_lag_ms

    def _last_message_age_seconds(self, endpoint: str | None = None) -> float | None:
        """Get age of last received message in seconds."""
        now = time.monotonic()
        if endpoint is not None:
            last_message_ts = self._last_message_ts_by_endpoint.get(endpoint, 0.0)
            if last_message_ts == 0.0:
                return None
            return round(now - last_message_ts, 1)
        ages = [now - ts for ts in self._last_message_ts_by_endpoint.values() if ts > 0.0]
        if ages:
            return round(min(ages), 1)
        if self._last_message_ts == 0.0:
            return None
        return round(now - self._last_message_ts, 1)

    async def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to market data for the given symbols.

        Args:
            symbols: List of trading symbols to subscribe to.
        """
        async with self._lock:
            current = set(self._symbols)
            requested_symbols = self._normalize_symbol_list(list(symbols))
            requested_set = set(requested_symbols)
            new_symbols = [s for s in requested_symbols if s not in current]
            removed_symbols = [s for s in self._symbols if s not in requested_set]
            self._symbols = requested_symbols
            previous_by_endpoint = {
                endpoint: set(streams)
                for endpoint, streams in self._intended_streams_by_endpoint.items()
            }
            self._recompute_intended_streams()
            current_by_endpoint = {
                endpoint: set(streams)
                for endpoint, streams in self._intended_streams_by_endpoint.items()
            }
        if removed_symbols:
            async with self._data_lock:
                for symbol in removed_symbols:
                    self._klines.pop(symbol, None)
                    self._book.pop(symbol, None)
                    self._agg_trades.pop(symbol, None)
                    self._depth_book.pop(symbol, None)
                    self._depth_update_times.pop(symbol, None)
                    self._depth_wall_state.pop(symbol, None)
                    self._depth_wall_pressure.pop(symbol, None)
                    self._book_update_times.pop(symbol, None)

        if not new_symbols and not removed_symbols:
            return

        if self._running:
            for endpoint in _WS_ENDPOINTS:
                removed_streams = list(
                    previous_by_endpoint[endpoint] - current_by_endpoint[endpoint]
                )
                added_streams = list(current_by_endpoint[endpoint] - previous_by_endpoint[endpoint])
                if removed_streams:
                    await self._send_subscription_command(endpoint, "UNSUBSCRIBE", removed_streams)
                if added_streams:
                    stream_task = self._stream_tasks[endpoint]
                    if stream_task is None or stream_task.done():
                        self._stream_tasks[endpoint] = asyncio.create_task(
                            self._run_stream(endpoint),
                            name=f"ws_manager_stream:{endpoint}",
                        )
                        if endpoint == _WS_MARKET:
                            self._stream_task = self._stream_tasks[endpoint]
                    else:
                        await self._send_subscription_command(endpoint, "SUBSCRIBE", added_streams)
            LOG.info(
                "ws_manager subscription update | added=%d removed=%d total=%d endpoints=%s",
                len(new_symbols),
                len(removed_symbols),
                len(requested_symbols),
                list(self._active_endpoint_classes()),
            )
            if new_symbols:
                self._schedule_backfill(
                    new_symbols,
                    name=f"ws_backfill_subscribe:{len(new_symbols)}",
                )
            return

        if new_symbols:
            self._schedule_backfill(
                new_symbols,
                name=f"ws_backfill_subscribe:{len(new_symbols)}",
            )

    def _base_streams_for_symbols(self, symbols: list[str]) -> list[str]:
        return base_streams_for_symbols(self, symbols)

    def _public_streams_for_symbols(self, symbols: list[str]) -> list[str]:
        return public_streams_for_symbols(self, symbols)

    def _stream_endpoint_class(self, stream: str) -> str:
        return stream_endpoint_class(stream)

    def _recompute_intended_streams(self) -> None:
        recompute_intended_streams(self)

    def _tracked_agg_trade_streams(self, symbols: list[str]) -> list[str]:
        return tracked_agg_trade_streams(self, symbols)

    async def set_tracked_symbols(self, symbols: list[str]) -> None:
        tracked_symbols = self._normalize_symbol_list(list(symbols))
        async with self._lock:
            requested = set(tracked_symbols)
            removed = [s for s in self._tracked_symbols if s not in requested]
            self._tracked_symbols = tracked_symbols
            previous_public_streams = set(self._intended_streams_by_endpoint[_WS_PUBLIC])
            previous_market_streams = set(self._intended_streams_by_endpoint[_WS_MARKET])
            self._recompute_intended_streams()
            current_public_streams = set(self._intended_streams_by_endpoint[_WS_PUBLIC])
            current_market_streams = set(self._intended_streams_by_endpoint[_WS_MARKET])

        if getattr(self._cfg, "subscribe_depth", False):
            public_task = self._stream_tasks[_WS_PUBLIC]
            if (
                self._running
                and (public_task is None or public_task.done())
                and current_public_streams
            ):
                self._stream_tasks[_WS_PUBLIC] = asyncio.create_task(
                    self._run_stream(_WS_PUBLIC),
                    name="ws_manager_stream:public",
                )
            elif self._ws_conns[_WS_PUBLIC] is not None and self._running:
                await self._send_subscription_command(
                    _WS_PUBLIC,
                    "UNSUBSCRIBE",
                    list(previous_public_streams - current_public_streams),
                )
                await self._send_subscription_command(
                    _WS_PUBLIC,
                    "SUBSCRIBE",
                    list(current_public_streams - previous_public_streams),
                )

        if not self._cfg.subscribe_agg_trade:
            if removed:
                async with self._data_lock:
                    for symbol in removed:
                        self._depth_book.pop(symbol, None)
                        self._depth_update_times.pop(symbol, None)
                        self._depth_wall_state.pop(symbol, None)
                        self._depth_wall_pressure.pop(symbol, None)
            return
        market_task = self._stream_tasks[_WS_MARKET]
        if self._running and (market_task is None or market_task.done()) and current_market_streams:
            self._stream_tasks[_WS_MARKET] = asyncio.create_task(
                self._run_stream(_WS_MARKET),
                name="ws_manager_stream:market",
            )
            self._stream_task = self._stream_tasks[_WS_MARKET]
        elif self._ws_conns[_WS_MARKET] is not None and self._running:
            await self._send_subscription_command(
                _WS_MARKET,
                "UNSUBSCRIBE",
                list(previous_market_streams - current_market_streams),
            )
            await self._send_subscription_command(
                _WS_MARKET,
                "SUBSCRIBE",
                list(current_market_streams - previous_market_streams),
            )
        if removed:
            async with self._data_lock:
                for symbol in removed:
                    self._agg_trades.pop(symbol, None)
                    self._depth_book.pop(symbol, None)
                    self._depth_update_times.pop(symbol, None)
                    self._depth_wall_state.pop(symbol, None)
                    self._depth_wall_pressure.pop(symbol, None)

        if tracked_symbols:
            LOG.info(
                "ws tracked symbols updated | tracked=%d agg_trade=%s",
                len(tracked_symbols),
                self._cfg.subscribe_agg_trade,
            )
        else:
            LOG.info("ws tracked symbols updated | tracked=0 aggTrade unsubscribed")

    async def _send_subscription_command(
        self, endpoint: str, method: str, streams: list[str]
    ) -> None:
        await send_subscription_command(self, endpoint, method, streams)

    def _global_streams(self) -> list[str]:
        """Return the list of market-wide streams to subscribe if enabled."""
        return global_streams(self)

    async def _resubscribe_all(self, endpoint: str, ws: Any) -> None:
        await resubscribe_all(self, endpoint, ws)

    def _apply_tcp_keepalive(self, ws: Any) -> None:
        apply_tcp_keepalive(self, ws)

    async def _health_monitor(self, ws: Any, endpoint: str) -> None:
        """Monitor WebSocket health and reconnect on silence/recovery failures."""
        while True:
            try:
                await asyncio.sleep(_HEALTH_CHECK_INTERVAL_SECONDS)
                if await monitor_connection_silence(self, ws, endpoint):
                    return
                if await evaluate_endpoint_health(self, ws, endpoint):
                    return
            except asyncio.CancelledError:
                raise
            except DEFENSIVE_EXC as exc:
                LOG.warning("health_monitor_error", extra={"endpoint": endpoint, "exc": str(exc)})

    def _kline_close_age_seconds(self, symbol: str, interval: str) -> float | None:
        deq = self._klines.get(symbol, {}).get(interval)
        if not deq:
            return None
        close_time = deq[-1].get("close_time")
        if close_time is None:
            return None
        try:
            if isinstance(close_time, str):
                close_ts = datetime.fromisoformat(close_time)
            else:
                close_ts = close_time
            return max(0.0, (datetime.now(UTC) - close_ts).total_seconds())
        except (ValueError, TypeError):
            return None

    def _stale_kline_streams(self) -> list[str]:
        stale: list[str] = []
        for symbol in self._symbols:
            for interval in self._cfg.kline_intervals:
                close_age = self._kline_close_age_seconds(symbol, interval)
                if close_age is None:
                    continue
                max_age = _INTERVAL_SECONDS.get(interval, 900) * 6
                if close_age > max_age:
                    stream_key = f"{symbol}:{interval}"
                    now = time.monotonic()
                    last_logged = self._last_stale_warning_by_stream.get(stream_key, 0.0)
                    if now - last_logged >= _HEALTH_CHECK_INTERVAL_SECONDS:
                        self._last_stale_warning_by_stream[stream_key] = now
                    stale.append(stream_key)
        return stale

    def _is_interval_fresh(self, symbol: str, interval: str) -> bool:
        close_age = self._kline_close_age_seconds(symbol, interval)
        if close_age is None:
            return False
        max_age = _INTERVAL_SECONDS.get(interval, 900) * 6
        return close_age <= max_age

    def _stale_symbols(self) -> list[str]:
        """Return list of symbols with stale (non-fresh) kline data."""
        return [
            symbol
            for symbol in self._symbols
            if any(
                not self._is_interval_fresh(symbol, interval)
                for interval in self._cfg.kline_intervals
            )
        ]

    def _should_backfill_after_disconnect(
        self, *, elapsed: float, stale_symbols: list[str]
    ) -> bool:
        if not stale_symbols:
            return False
        self._last_short_disconnect_s = (
            round(elapsed, 1) if elapsed < _SHORT_DISCONNECT_BACKFILL_GRACE_SECONDS else None
        )
        if elapsed >= _SHORT_DISCONNECT_BACKFILL_GRACE_SECONDS:
            return True
        return any(symbol not in self._klines for symbol in stale_symbols)

    async def _maybe_backfill_after_disconnect(
        self, *, elapsed: float, stale_symbols: list[str]
    ) -> None:
        if not stale_symbols:
            return
        if not self._should_backfill_after_disconnect(elapsed=elapsed, stale_symbols=stale_symbols):
            LOG.info(
                "reconnect backfill skipped | short_disconnect=%.1fs stale_symbols=%d",
                elapsed,
                len(stale_symbols),
            )
            return
        LOG.info("reconnect backfill | stale_symbols=%d", len(stale_symbols))
        try:
            await self._backfill(stale_symbols)
        except (
            ConnectionError,
            TimeoutError,
            ValueError,
            RuntimeError,
        ) as backfill_exc:
            LOG.debug("reconnect backfill failed (non-fatal): %s", backfill_exc)

    def is_warm(self, symbol: str) -> bool:
        """Check if all required data is available and fresh for a symbol.

        Args:
            symbol: Trading symbol to check.

        Returns:
            True if symbol has all required fresh data.
        """
        klines = self._klines.get(symbol, {})
        for interval in self._cfg.kline_intervals:
            deq = klines.get(interval)
            if not deq:
                return False
            if not self._is_interval_fresh(symbol, interval):
                return False
        return not (self._cfg.subscribe_book_ticker and symbol not in self._book)

    async def get_symbol_frames(self, symbol: str) -> SymbolFrames | None:
        """Get structured market data frames for a symbol.

        Acquires _data_lock to prevent races with kline/book writers.

        Args:
            symbol: Trading symbol to retrieve data for.

        Returns:
            SymbolFrames object with kline data and book prices, or None if not warm.
        """
        async with self._data_lock:
            if not self.is_warm(symbol):
                return None
            klines = self._klines.get(symbol, {})
            interval_dfs: dict[str, pl.DataFrame] = {}
            for interval in self._cfg.kline_intervals:
                deq = klines.get(interval)
                if not deq:
                    return None
                interval_dfs[interval] = pl.DataFrame(list(deq))

            bid, ask = self._book.get(symbol, (None, None))
            bid_qty, ask_qty = self._book_qty.get(symbol, (None, None))
        return SymbolFrames(
            symbol=symbol,
            df_1h=interval_dfs.get("1h", pl.DataFrame()),
            df_15m=interval_dfs.get("15m", pl.DataFrame()),
            bid_price=bid,
            ask_price=ask,
            df_5m=interval_dfs.get("5m", pl.DataFrame()),
            df_4h=interval_dfs.get("4h", pl.DataFrame()),
            bid_qty=bid_qty,
            ask_qty=ask_qty,
        )

    async def get_book_ticker(self, symbol: str) -> tuple[float | None, float | None] | None:
        """Get current best bid/ask prices for a symbol.

        Acquires _data_lock to prevent races with book ticker writers.

        Args:
            symbol: Trading symbol to retrieve prices for.

        Returns:
            Tuple of (bid, ask) prices or None if unavailable.
        """
        async with self._data_lock:
            return self._book.get(symbol)

    def get_agg_trade_snapshot(
        self, symbol: str, *, window_seconds: int | None = None
    ) -> AggTradeSnapshot | None:
        """Get aggregated trade statistics for a symbol over the configured window.

        Args:
            symbol: Trading symbol to retrieve trade snapshot for.

        Returns:
            AggTradeSnapshot with buy/sell statistics or None if no recent trades.
        """
        buf = self._agg_trades.get(symbol)
        if not buf:
            return None
        window = int(window_seconds or self._cfg.agg_trade_window_seconds)
        cutoff_ms = int(time.time() * 1000) - window * 1000
        buy_qty = sell_qty = 0.0
        count = 0
        for trade in buf:
            if trade.trade_time_ms < cutoff_ms:
                continue
            count += 1
            if trade.is_buyer_maker:
                sell_qty += trade.quantity
            else:
                buy_qty += trade.quantity
        if count == 0:
            return None
        total = buy_qty + sell_qty
        delta_ratio = (buy_qty - sell_qty) / total if total > 0 else None
        return AggTradeSnapshot(
            symbol=symbol,
            trade_count=count,
            buy_qty=buy_qty,
            sell_qty=sell_qty,
            delta_ratio=delta_ratio,
        )

    def get_depth_book_snapshot(
        self, symbol: str
    ) -> dict[str, tuple[tuple[float, float], ...]] | None:
        """Return the latest partial L2 book for a symbol, if available."""
        return self._depth_book.get(symbol)

    def get_depth_book_age_seconds(self, symbol: str) -> float | None:
        updated_at = self._depth_update_times.get(symbol)
        if updated_at is None or updated_at <= 0.0:
            return None
        return round(time.monotonic() - updated_at, 3)

    def get_depth_wall_pressure(self, symbol: str) -> float | None:
        """Return signed persistent wall pressure from partial depth, if available."""
        return self._depth_wall_pressure.get(symbol)

    # ------------------------------------------------------------------ #
    # Global market stream accessors                                       #
    # ------------------------------------------------------------------ #

    def is_ticker_cache_warm(self) -> bool:
        """Return True if the !ticker@arr cache has been populated recently.

        Uses ``ws.market_ticker_freshness_seconds`` as the staleness limit.
        Falls back to True only if the cache has at least a handful of symbols
        so the shortlist can be meaningful.
        """
        return is_ticker_cache_warm(self)

    def get_stats(self) -> dict[str, Any]:
        """P3: Return WebSocket statistics for monitoring.

        Includes:
        - Buffer stats (dropped/processed messages)
        - Stream count
        - Slow streams (avg latency > 5000ms)
        - Per-stream average latency
        """
        return get_stats(self)

    def get_ticker_snapshot(self, symbol: str) -> JsonDict | None:
        """Return the latest 24hr ticker dict for *symbol*, or None."""
        return self._ticker_cache.get(symbol)

    def get_ticker_age_seconds(self, symbol: str) -> float | None:
        updated_at = self._ticker_update_times.get(symbol)
        if updated_at is None or updated_at <= 0.0:
            return None
        return round(time.monotonic() - updated_at, 3)

    def get_kline_cache(self, symbol: str, interval: str) -> list[JsonDict] | None:
        """Return the most recent kline rows for *symbol*/*interval* as a list.

        Returns None if no data is cached yet. Rows are dicts with keys:
        time, open, high, low, close, volume, close_time, etc.
        """
        deq = self._klines.get(symbol, {}).get(interval)
        if not deq:
            return None
        return list(deq)

    def get_global_ticker_data(self) -> list[JsonDict]:
        """Return all cached tickers in the format expected by build_shortlist.

        Each dict contains: symbol, quote_volume, price_change_percent,
        last_price (keys used by universe.build_shortlist).

        This method prefers full 24hr ticker data (!ticker@arr) but falls back
        to miniTicker (!miniTicker@arr) if available and full ticker is stale.
        """
        return get_global_ticker_data(self)

    def get_mark_price_snapshot(self, symbol: str) -> JsonDict | None:
        """Return the latest mark-price/funding dict for *symbol*, or None.

        Returned dict keys: ``mark_price`` (float), ``funding_rate`` (float),
        ``next_funding_time_ms`` (int).
        """
        return self._mark_price_cache.get(symbol)

    def get_mark_price_age_seconds(self, symbol: str) -> float | None:
        updated_at = self._mark_price_update_times.get(symbol)
        if updated_at is None or updated_at <= 0.0:
            return None
        return round(time.monotonic() - updated_at, 3)

    def get_book_snapshot(self, symbol: str) -> tuple[float | None, float | None]:
        """Return the latest (bid_price, ask_price) for *symbol*, or (None, None)."""
        return self._book.get(symbol, (None, None))

    def get_book_ticker_age_seconds(self, symbol: str) -> float | None:
        updated_at = self._book_update_times.get(symbol)
        if updated_at is None or updated_at <= 0.0:
            return None
        return round(time.monotonic() - updated_at, 3)

    def get_depth_imbalance(self, symbol: str) -> float | None:
        """Return L2 depth imbalance in [-1, 1], falling back to L1/flow proxy."""
        return get_depth_imbalance(self, symbol)

    def get_depth_imbalance_source(self, symbol: str) -> str | None:
        """Return the data source backing the latest depth imbalance value."""
        return get_depth_imbalance_source(self, symbol)

    def get_microprice_bias(self, symbol: str) -> float | None:
        """Calculate microprice bias from order book.

        Returns a signed bias proxy in [-1, 1], where positive means buy pressure
        and negative means sell pressure.

        Uses best bid/ask quantities from bookTicker or partial depth; falls
        back to recent aggTrade delta when the book is unavailable.
        """
        return get_microprice_bias(self, symbol)

    def get_microprice_bias_source(self, symbol: str) -> str | None:
        """Return the data source backing the latest microprice bias value."""
        return get_microprice_bias_source(self, symbol)

    def get_funding_sentiment(self) -> float | None:
        """Return the average funding rate across all tracked symbols.

        Positive → market is net-long / bullish crowding.
        Negative → market is net-short / bearish crowding.
        Returns None if the mark-price cache is empty.
        """
        return get_funding_sentiment(self)

    def get_liquidation_sentiment(
        self,
        symbol: str | None = None,
        window_seconds: int = 60,
    ) -> float | None:
        """Return a liquidation sentiment score in [-1.0, +1.0].

        +1.0 → all recent liquidations were SHORT (buy-side squeeze, bullish).
        -1.0 → all recent liquidations were LONG (sell-side squeeze, bearish).
        Returns None if no liquidations in the window.

        Args:
            symbol: If given, filter to this symbol only.
            window_seconds: Look-back window in seconds.
        """
        return get_liquidation_sentiment(
            self, symbol=symbol, window_seconds=window_seconds
        )

    def get_liquidation_rollups(
        self,
        symbol: str | None = None,
        window_seconds: int = 900,
    ) -> dict[str, float] | None:
        """Notional rollups for liquidation heatmap / positioning context."""
        return get_liquidation_rollups(
            self, symbol=symbol, window_seconds=window_seconds
        )

    def get_liquidation_age_seconds(
        self,
        symbol: str | None = None,
        window_seconds: int = 60,
    ) -> float | None:
        """Return age of the newest forceOrder event used for liquidation sentiment."""
        return get_liquidation_age_seconds(
            self, symbol=symbol, window_seconds=window_seconds
        )

    async def rebuild_shortlist_on_demand(
        self,
        symbol_meta: list[Any],
        settings: Any,
    ) -> tuple[list[Any], dict[str, Any]]:
        """Rebuild shortlist using WS cache with timer-based throttling.

        This method is called from app.run_cycle() and implements the requested
        60-90 second rebuild interval to reduce load while keeping data fresh.

        Args:
            symbol_meta: List of exchange symbol metadata objects
            settings: Bot settings (for universe configuration)

        Returns:
            Tuple of (shortlist, summary_dict) compatible with universe.build_shortlist
        """
        now = time.monotonic()
        async with self._shortlist_rebuild_lock:
            time_since_last = now - self._last_shortlist_rebuild_ts
            if time_since_last < self._shortlist_rebuild_interval_seconds:
                LOG.debug(
                    "shortlist rebuild throttled | age=%.1fs < interval=%.1fs",
                    time_since_last,
                    self._shortlist_rebuild_interval_seconds,
                )
                return self._last_shortlist, self._last_shortlist_summary

            self._last_shortlist_rebuild_ts = now
            LOG.debug("shortlist rebuild triggered | age=%.1fs", time_since_last)

            if self.is_ticker_cache_warm():
                tickers = self.get_global_ticker_data()
                LOG.debug("shortlist from WS cache | symbols=%d", len(tickers))
            else:
                # Fall back to empty - caller should use REST
                LOG.debug("shortlist WS cache cold, signaling REST fallback needed")
                tickers = []

            shortlist, summary = build_shortlist(
                symbol_meta,
                tickers,
                settings,
                seed_source="ws_light",
            )
            self._last_shortlist = shortlist
            self._last_shortlist_summary = summary
            return shortlist, summary

    def _should_throttle_ticker_update(self, symbol: str) -> bool:
        """Check if ticker update should be throttled (debounce rapid updates)."""
        return should_throttle_ticker_update(self, symbol)

    def _should_throttle_mark_price_update(self, symbol: str) -> bool:
        """Check if mark price update should be throttled."""
        return should_throttle_mark_price_update(self, symbol)

    async def _backfill(self, symbols: list[str]) -> None:
        now = time.monotonic()
        filtered_symbols: list[str] = []
        for symbol in symbols:
            cooldown_until = self._backfill_cooldowns.get(symbol)
            if cooldown_until is not None and now < cooldown_until:
                LOG.info(
                    "backfill cooldown active | symbol=%s remaining=%.1fs",
                    symbol,
                    cooldown_until - now,
                )
                continue
            filtered_symbols.append(symbol)
        tasks = [
            self._backfill_one(symbol, interval)
            for symbol in filtered_symbols
            for interval in self._cfg.kline_intervals
        ]
        if self._cfg.subscribe_book_ticker:
            tasks.extend(self._backfill_book_ticker(symbol) for symbol in filtered_symbols)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _backfill_one(self, symbol: str, interval: str) -> None:
        try:
            async with self._backfill_sem:
                df = await self._rest.fetch_klines(
                    symbol, interval, limit=self._cfg.kline_cache_size
                )
            if df is None or df.is_empty():
                LOG.debug("backfill_empty_frame", extra={"symbol": symbol, "interval": interval})
                return
            rows = df.to_dicts()
            deq: collections.deque[JsonDict] = collections.deque(
                rows, maxlen=self._cfg.kline_cache_size
            )
            async with self._data_lock:
                if symbol not in self._klines:
                    self._klines[symbol] = {}
                self._klines[symbol][interval] = deq
            self._backfill_cooldowns.pop(symbol, None)
        except (
            ConnectionError,
            TimeoutError,
            ValueError,
            RuntimeError,
            MarketDataUnavailable,
        ) as exc:
            if self._cfg.backfill_failure_cooldown_seconds > 0:
                self._backfill_cooldowns[symbol] = (
                    time.monotonic() + self._cfg.backfill_failure_cooldown_seconds
                )
            LOG.debug("backfill failed | symbol=%s interval=%s: %s", symbol, interval, exc)

    async def _backfill_book_ticker(self, symbol: str) -> None:
        try:
            async with self._backfill_sem:
                detail = await self._rest._fetch_book_ticker_rest_detail(symbol)
            bid = detail.get("bid_price")
            ask = detail.get("ask_price")
            bid_qty = detail.get("bid_qty")
            ask_qty = detail.get("ask_qty")
            async with self._data_lock:
                self._book[symbol] = (bid, ask)
                if bid_qty is not None and ask_qty is not None:
                    self._book_qty[symbol] = (float(bid_qty), float(ask_qty))
                else:
                    self._book_qty.pop(symbol, None)
            self._backfill_cooldowns.pop(symbol, None)
        except (
            ConnectionError,
            TimeoutError,
            ValueError,
            RuntimeError,
            MarketDataUnavailable,
        ) as exc:
            if self._cfg.backfill_failure_cooldown_seconds > 0:
                self._backfill_cooldowns[symbol] = (
                    time.monotonic() + self._cfg.backfill_failure_cooldown_seconds
                )
            LOG.debug("book ticker backfill failed | symbol=%s: %s", symbol, exc)

    def _get_ws_fallback_urls(self, endpoint: str) -> list[str]:
        """Return the WebSocket URL list for a single endpoint class.

        Cross-endpoint fallback is intentionally disabled so market streams
        cannot drift onto `public` and public streams cannot drift onto `market`.
        """
        return get_ws_fallback_urls(self, endpoint)

    def _build_stream_url(self, endpoint: str) -> str:
        return build_stream_url(self, endpoint)

    def _get_ws_url_version(self, endpoint: str) -> str:
        return get_ws_url_version(self, endpoint)

    def _clear_endpoint_connection_state(self, endpoint: str) -> None:
        clear_endpoint_connection_state(self, endpoint)

    async def _run_stream(self, endpoint: str) -> None:
        await run_endpoint_loop(
            self,
            endpoint=endpoint,
            backoff_reset_after_seconds=_BACKOFF_RESET_AFTER_SECONDS,
            proactive_reconnect_after_seconds=_PROACTIVE_RECONNECT_AFTER_SECONDS,
            parse_message=_json.loads,
            market_endpoint=_WS_MARKET,
            stale_symbols=self._stale_symbols,
            maybe_backfill_after_disconnect=self._maybe_backfill_after_disconnect,
            compute_disconnect_delay=compute_disconnect_delay,
            connection_exceptions=(
                ws_exceptions.ConnectionClosed,
                ws_exceptions.InvalidStatus,
                ConnectionError,
                TimeoutError,
                OSError,
            ),
        )

    def _handle_ws_response(self, msg: JsonDict, endpoint: str) -> None:
        if msg.get("error"):
            self._subscription_errors[endpoint] = msg["error"]
            LOG.error(
                "ws subscription error from Binance | endpoint=%s error=%s",
                endpoint,
                msg["error"],
            )
            return
        # Parse rate limits from response (Binance includes rateLimits in WS API responses)
        if "rateLimits" in msg:
            try:
                rate_limits = msg["rateLimits"]
                if isinstance(rate_limits, list):
                    for limit in rate_limits:
                        limit_type = limit.get("rateLimitType")
                        interval = limit.get("interval")
                        limit_val = limit.get("limit")
                        count = limit.get("count")
                        if limit_type and count is not None and limit_val is not None:
                            usage_pct = (count / limit_val) * 100 if limit_val > 0 else 0
                            if usage_pct > 95:
                                LOG.error(
                                    (
                                        "ws rate limit high usage | endpoint=%s type=%s "
                                        "interval=%s count=%d limit=%d usage=%.1f%%"
                                    ),
                                    endpoint,
                                    limit_type,
                                    interval,
                                    count,
                                    limit_val,
                                    usage_pct,
                                )
                            elif usage_pct > 80:
                                LOG.info(
                                    (
                                        "ws rate limit elevated usage | endpoint=%s type=%s "
                                        "interval=%s count=%d limit=%d usage=%.1f%%"
                                    ),
                                    endpoint,
                                    limit_type,
                                    interval,
                                    count,
                                    limit_val,
                                    usage_pct,
                                )
            except (TypeError, ValueError, KeyError) as exc:
                LOG.debug("failed to parse ws rate limit payload from %s: %s", endpoint, exc)
        if "result" not in msg:
            return
        result = msg["result"]
        if isinstance(result, list):
            self._subscription_ack_count[endpoint] += 1
            expected = len(self._intended_streams_by_endpoint.get(endpoint, set()))
            actual = len(result)
            if expected != actual:
                LOG.debug(
                    "ws subscriptions mismatch | endpoint=%s expected=%d actual=%d",
                    endpoint,
                    expected,
                    actual,
                )
            else:
                LOG.debug(
                    "ws subscriptions confirmed | endpoint=%s active=%d",
                    endpoint,
                    actual,
                )
        else:
            self._subscription_ack_count[endpoint] += 1
            LOG.debug("ws command ack | endpoint=%s id=%s", endpoint, msg.get("id"))

    async def _dispatch_event(self, data: JsonDict) -> None:
        """Dispatch a single market data event dict to the appropriate handler.

        kline/bookTicker/aggTrade handlers are async and acquire _data_lock to
        prevent races with subscribe() cleanup.  ticker/markPrice/forceOrder
        handlers are fine-grained and do not need the lock.
        """
        event_type = str(data.get("e", ""))
        symbol = str(data.get("s", "")).upper()
        event_time = data.get("E")
        if event_time is not None:
            try:
                self._last_event_lag_ms = max(0.0, self._now_epoch_ms() - float(event_time))
                if (
                    symbol
                    and event_type in _LATENCY_WARNING_EVENTS
                    and self._last_event_lag_ms >= _HIGH_EVENT_LATENCY_MS
                ):
                    now = time.monotonic()
                    last_warn = self._last_latency_warning_by_symbol.get(symbol, 0.0)
                    if now - last_warn >= _LATENCY_WARNING_INTERVAL_SECONDS:
                        self._last_latency_warning_by_symbol[symbol] = now
                        LOG.debug(
                            "ws high latency | lag_ms=%.1f symbol=%s event=%s",
                            self._last_event_lag_ms,
                            symbol,
                            event_type,
                        )
            except (TypeError, ValueError):
                pass
        if event_type == "kline":
            if symbol:
                await self._handle_kline(symbol, data)
        elif event_type == "bookTicker":
            if symbol:
                await self._handle_book_ticker(symbol, data)
        elif event_type == "depthUpdate":
            if symbol:
                await self._handle_depth_update(symbol, data)
        elif event_type == "aggTrade":
            if symbol:
                await self._handle_agg_trade(symbol, data)
        elif event_type == "24hrTicker":
            if symbol:
                self._handle_ticker(symbol, data)
        elif event_type == "markPriceUpdate":
            if symbol:
                self._handle_mark_price(symbol, data)
        elif event_type == "miniTicker":
            if symbol:
                self._handle_mini_ticker(symbol, data)
        elif event_type == "forceOrder":
            self._handle_force_order(data)

    async def _handle_message(self, msg: JsonDict, endpoint: str) -> None:
        self._last_message_ts = time.monotonic()
        self._last_message_ts_by_endpoint[endpoint] = self._last_message_ts
        if "result" in msg or "error" in msg:
            self._handle_ws_response(msg, endpoint)
            return

        data = msg.get("data")
        stream = str(msg.get("stream") or "")
        if isinstance(data, dict) and data.get("e") == "kline":
            kline = data.get("k", {})
            if isinstance(kline, dict) and not kline.get("x"):
                return

        if _is_global_market_stream(stream):
            await self._process_message_internal(msg)
            return

        buffered = await self._message_buffer.put(msg)
        if not buffered:
            # Backpressure fallback: process directly to avoid full data starvation.
            await self._process_message_internal(msg)

    async def _process_message_internal(self, msg: JsonDict) -> None:
        """Internal message processing with latency tracking."""
        start_ts = time.monotonic()
        data = msg.get("data")
        stream = msg.get("stream", "unknown")
        if isinstance(data, dict):
            order = data.get("o")
            str(
                data.get("s")
                or (order.get("s") if isinstance(order, dict) else None)
                or str(stream).split("@", 1)[0]
                or "unknown"
            ).upper()
        # Binance: ≤10 incoming messages/s per connection — pace, do not drop market data.
        await self._rate_limiter.wait_for_slot()

        # P3: Per-stream latency tracking
        if stream not in self._stream_latency_ms:
            self._stream_latency_ms[stream] = collections.deque(maxlen=10)

        # !ticker@arr and !markPrice@arr deliver a LIST of event objects.
        # Process all items to populate global caches, but only dispatch events
        # for symbols in our shortlist to avoid unnecessary processing.
        if isinstance(data, list):
            symbol_set = set(self._symbols)
            count = 0
            for item in data:
                if not isinstance(item, dict):
                    continue
                event_type = str(item.get("e", ""))
                sym = str(item.get("s", "")).upper()
                if self._should_drop_stale_event(event_type, item.get("E")):
                    self._stale_event_drop_count += 1
                    continue
                # forceOrder arrives as a wrapper; inner symbol is in item["o"]["s"]
                if event_type == "forceOrder":
                    await self._dispatch_event(item)
                elif event_type == "24hrTicker":
                    # Save ALL tickers to cache for shortlist building;
                    # dispatch only for shortlist symbols
                    self._handle_ticker(sym, item)
                    if sym in symbol_set:
                        await self._dispatch_event(item)
                elif event_type == "markPriceUpdate":
                    # Save ALL mark prices to cache
                    self._handle_mark_price(sym, item)
                    if sym in symbol_set:
                        await self._dispatch_event(item)
                elif sym and sym in symbol_set:
                    await self._dispatch_event(item)
                count += 1
                if count % 250 == 0:
                    await asyncio.sleep(0)

            # Record latency for batch
            latency_ms = (time.monotonic() - start_ts) * 1000
            self._stream_latency_ms[stream].append(latency_ms)
            return

        # Per-symbol streams and !forceOrder@arr deliver a single dict
        if isinstance(data, dict):
            if self._should_drop_stale_event(str(data.get("e", "")), data.get("E")):
                self._stale_event_drop_count += 1
                return
            await self._dispatch_event(data)
            # Record latency
            latency_ms = (time.monotonic() - start_ts) * 1000
            self._stream_latency_ms[stream].append(latency_ms)

            # P3: Check if stream is slow (avg latency > 5000ms)
            if len(self._stream_latency_ms[stream]) >= 5:
                avg_latency = sum(self._stream_latency_ms[stream]) / len(
                    self._stream_latency_ms[stream]
                )
                if avg_latency > 5000 and stream not in self._slow_streams:
                    self._slow_streams.add(stream)
                    LOG.error(
                        "slow stream detected | stream=%s avg_latency_ms=%.1f",
                        stream,
                        avg_latency,
                    )

    async def _process_buffered_messages(self) -> None:
        """Background task to drain buffered WS messages quickly."""
        while self._running:
            try:
                processed = 0
                while processed < 1000:
                    msg = await self._message_buffer.get()
                    if msg is None:
                        break
                    await self._process_message_internal(msg)
                    processed += 1
                if processed == 0:
                    await asyncio.sleep(0.01)
                else:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except DEFENSIVE_EXC as exc:
                LOG.debug("buffer processor error: %s", exc)
                await asyncio.sleep(0.5)

    def _should_drop_stale_event(self, event_type: str, event_time_ms: Any) -> bool:
        if event_type not in _STALE_DROP_EVENTS:
            return False
        try:
            event_ms = float(event_time_ms)
        except (TypeError, ValueError):
            return False
        if event_ms <= 0:
            return False
        if event_type == "aggTrade":
            max_age_seconds = float(getattr(self._cfg, "agg_trade_freshness_seconds", 300.0))
        else:
            max_age_seconds = float(getattr(self._cfg, "market_ticker_freshness_seconds", 30.0))
        age_ms = self._now_epoch_ms() - event_ms
        return age_ms > (max_age_seconds * 1000.0)

    def _now_epoch_ms(self) -> float:
        return time.monotonic() * 1000.0 + self._epoch_monotonic_offset_ms

    async def _handle_kline(self, symbol: str, data: JsonDict) -> None:
        """Handle closed kline (candle) events.  Acquires _data_lock to prevent
        races with subscribe() cleanup of self._klines."""
        if self._symbols and symbol not in self._symbols:
            return
        k = data.get("k", {})
        if not k.get("x"):
            return
        interval = str(k.get("i", ""))
        LOG.debug("kline closed | symbol=%s interval=%s", symbol, interval)
        if interval not in self._cfg.kline_intervals:
            return
        row = _ws_kline_to_row(k)
        async with self._data_lock:
            if symbol not in self._klines:
                self._klines[symbol] = {}
            if interval not in self._klines[symbol]:
                self._klines[symbol][interval] = collections.deque(
                    maxlen=self._cfg.kline_cache_size
                )
            deq = self._klines[symbol][interval]
            # Gap detection: fire before appending so backfill can repair the hole
            interval_secs = _INTERVAL_SECONDS.get(interval, 0)
            if interval_secs > 0 and deq:
                gap_secs = (row["time"] - deq[-1]["close_time"]).total_seconds()
                if gap_secs > interval_secs * 0.9:
                    missed = max(1, round(gap_secs / interval_secs) - 1)
                    LOG.error(
                        "kline gap detected | symbol=%s interval=%s "
                        "last_close=%s new_open=%s missed_candles=%d — triggering backfill",
                        symbol,
                        interval,
                        deq[-1]["close_time"],
                        row["time"],
                        missed,
                    )
                    self._schedule_backfill(
                        [symbol],
                        name=f"gap_backfill:{symbol}:{interval}",
                    )
            if deq and deq[-1].get("time") == row["time"]:
                LOG.debug(
                    "kline dedup update | symbol=%s interval=%s time=%s",
                    symbol,
                    interval,
                    row["time"],
                )
                deq[-1] = row
            else:
                deq.append(row)

        # Fire candle-close callbacks (fire-and-forget; data already in deque)
        close_ts_ms = int(k.get("T", 0))
        cbs = self._kline_close_cbs.get(interval)
        if cbs:
            for _cb in cbs:
                task = asyncio.create_task(_cb(symbol, interval, close_ts_ms))
                self._track_callback_task(task, label=f"kline_close:{symbol}:{interval}")

        # Publish to EventBus (primary path when SignalBot uses EventBus)
        if self._event_bus is not None:
            self._event_bus.publish_nowait(
                KlineCloseEvent(symbol=symbol, interval=interval, close_ts=close_ts_ms)
            )
            LOG.debug("kline published to EventBus | symbol=%s interval=%s", symbol, interval)
        else:
            if not self._event_bus_missing_logged:
                LOG.info("kline EventBus publish skipped because EventBus is not attached")
                self._event_bus_missing_logged = True

    async def _handle_book_ticker(self, symbol: str, data: JsonDict) -> None:
        """Handle bookTicker events.  Acquires _data_lock to prevent races."""
        await handle_book_ticker(self, symbol, data)

    async def _handle_depth_update(self, symbol: str, data: JsonDict) -> None:
        """Handle partial depth events for active orderflow symbols."""
        await handle_depth_update(self, symbol, data)

    async def _handle_agg_trade(self, symbol: str, data: JsonDict) -> None:
        """Handle aggTrade events.  Acquires _data_lock to prevent races."""
        await handle_agg_trade(self, symbol, data)

    def _handle_ticker(self, symbol: str, data: JsonDict) -> None:
        """Handle 24hrTicker events from !ticker@arr.

        Binance Futures 24hr ticker fields:
          c = last price, q = quote volume (24h), P = price change %,
          p = price change abs, o = open price, h = high, l = low.
        """
        handle_ticker(self, symbol, data)

    def _handle_mini_ticker(self, symbol: str, data: JsonDict) -> None:
        """Handle miniTicker events from !miniTicker@arr (lightweight fallback).

        miniTicker fields:
          c = last price, q = quote volume (24h), v = base volume,
          h = high, l = low, o = open.
        """
        handle_mini_ticker(self, symbol, data)

    def _handle_mark_price(self, symbol: str, data: JsonDict) -> None:
        """Handle markPriceUpdate events from !markPrice@arr@1s.

        Binance Futures mark price fields:
          p = mark price, r = funding rate, T = next funding time ms,
          i = index price.
        """
        handle_mark_price(self, symbol, data)

    def _handle_force_order(self, data: JsonDict) -> None:
        """Handle forceOrder (liquidation) events from !forceOrder@arr.

        The inner order object ``o`` carries: s=symbol, S=side (BUY/SELL),
        q=original qty, ap=average fill price, T=trade time ms.
        Side BUY = a short position was liquidated (bullish pressure).

        Note (2026-04-10): Binance changed from sending 'latest' to 'largest'
        liquidation order within 1000ms window. Buffer logic remains unchanged
        as we process individual events as received.
        Side SELL = a long position was liquidated (bearish pressure).
        """
        handle_force_order(self, data)

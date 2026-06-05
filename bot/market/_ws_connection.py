"""Private WebSocket connection and reconnect helpers (extracted from ws.py)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import socket
import time
from typing import Any

import websockets
from websockets import exceptions as ws_exceptions

from bot.market.network_proxy import websockets_connect_kwargs
from bot.runtime.errors import DEFENSIVE_EXC

LOG = logging.getLogger("bot.ws_manager")
_WS_CONNECT_TIMEOUT_SECONDS = 60.0
_WS_CLOSE_TIMEOUT_SECONDS = 10.0
# fix-20260604: module-local constants (ws.py imports this module; avoid circular import)
_BACKOFF_RESET_AFTER_SECONDS = 90.0
_WS_PING_INTERVAL_SECONDS = 20.0
_WS_PING_TIMEOUT_SECONDS = 60.0


def compute_disconnect_delay(
    manager: Any, *, endpoint: str, url: str, exc: Exception, elapsed: float, delay: float
) -> float:
    """Update streak/reconnect metadata and return next retry delay."""
    error_text = str(exc).lower()
    keepalive_timeout = "keepalive ping timeout" in error_text
    if elapsed < _BACKOFF_RESET_AFTER_SECONDS and (not keepalive_timeout):
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
    next_delay = min(300.0, next_delay + random.uniform(0.0, min(0.5, next_delay * 0.1)))
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
    grace_seconds = max(60.0, float(getattr(manager._cfg, "market_reconnect_grace_seconds", 60.0)))
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
                    # fix-loop-5: planned reconnect - warning only (not a crash)
                    LOG.warning(
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
                    "ws stale kline data | endpoint=%s streams=%d sample=%s "
                    "- backfilling (not reconnecting)"
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
            (
                1
                for sym, ts in manager._book_update_times.items()
                if sym in manager._symbols
                and time.monotonic() - ts <= manager._cfg.market_ticker_freshness_seconds
            )
        )
        if manager._symbols and manager._cfg.subscribe_book_ticker and (fresh_books == 0):
            connected_at = manager._connected_at_by_endpoint.get(endpoint, 0.0)
            if connected_at > 0.0 and time.monotonic() - connected_at >= grace_seconds:
                LOG.warning(
                    "ws public recovery failed | endpoint=%s fresh_book_tickers=0 "
                    "- forcing reconnect",
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
    grace_seconds = max(60.0, float(getattr(manager._cfg, "market_reconnect_grace_seconds", 60.0)))
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
            manager._health_monitor(ws, endpoint), name=f"ws_manager_health:{endpoint}"
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
        return (backoff_reset, True)
    if reconnect_reason == "shutdown" or graceful_close:
        manager._last_reconnect_reason = f"{endpoint}:graceful_close"
        manager._last_reconnect_reason_by_endpoint[endpoint] = "graceful_close"
        LOG.info("ws graceful close | endpoint=%s", endpoint)
        return (backoff_reset, False)
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
                        elapsed=time.monotonic() - connect_start, stale_symbols=stale
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
                    manager, endpoint=endpoint, url=url, exc=exc, elapsed=elapsed, delay=delay
                )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if retry_streak > 3:
                delay = min(delay * 2.0, max_delay)
            if endpoint == market_endpoint:
                stale = stale_symbols()
                await maybe_backfill_after_disconnect(elapsed=elapsed, stale_symbols=stale)
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

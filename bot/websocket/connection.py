"""Connection helpers extracted from ``bot.ws_manager``."""

from __future__ import annotations

import asyncio
import socket
import logging
import time
from typing import Any

import websockets

LOG = logging.getLogger("bot.ws_manager")


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


def apply_tcp_keepalive(manager: Any, ws: Any) -> None:
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
        asyncio.create_task(manager._reconnect_cb())

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
    ws = await asyncio.wait_for(
        websockets.connect(
            url,
            ping_interval=20.0,
            ping_timeout=20.0,
            close_timeout=10.0,
        ),
        timeout=10.0,
    )
    LOG.info("ws connection established | endpoint=%s url=%s", endpoint, url)
    backoff_reset = False
    reconnect_reason: str | None = None
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
                    return backoff_reset, False
                elapsed = time.monotonic() - connect_start
                if not backoff_reset and elapsed >= backoff_reset_after_seconds:
                    backoff_reset = True
                    manager._short_lived_streak = 0
                if elapsed >= proactive_reconnect_after_seconds:
                    reconnect_reason = "24h_proactive"
                    break
                try:
                    msg = parse_message(raw)
                except Exception:
                    continue
                await manager._handle_message(msg, endpoint)
        finally:
            health_task.cancel()
            try:
                await health_task
            except asyncio.CancelledError:
                pass

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
    raise ConnectionError("stream closed without explicit close frame")

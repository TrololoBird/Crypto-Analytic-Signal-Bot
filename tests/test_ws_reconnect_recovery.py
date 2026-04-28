from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot.config import WSConfig
from bot.websocket import subscriptions as ws_subscriptions
from bot.ws_manager import FuturesWSManager


class _DummyWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_resubscribe_all_uses_intended_streams_without_changing_market_binding() -> None:
    ws = _DummyWS()
    previous_market_ws = object()
    manager = SimpleNamespace(
        _cfg=SimpleNamespace(subscribe_chunk_size=50, subscribe_chunk_delay_ms=0),
        _running=True,
        _symbols=["BTCUSDT", "ETHUSDT"],
        _subscribe_id=1,
        _ws_conn=previous_market_ws,
        _ws_conns={"market": previous_market_ws},
        _intended_streams_by_endpoint={"market": {"btcusdt@kline_15m", "!ticker@arr"}},
    )

    await ws_subscriptions.resubscribe_all(manager, "market", ws)

    assert len(ws.sent) == 1
    assert manager._ws_conns["market"] is ws
    assert manager._ws_conn is ws


@pytest.mark.asyncio
async def test_recovery_backfill_skips_short_disconnect_when_cache_is_present() -> None:
    manager = FuturesWSManager(rest_client=SimpleNamespace(), config=WSConfig())
    manager._backfill = AsyncMock()
    manager._klines = {"BTCUSDT": {"15m": []}}

    await manager._maybe_backfill_after_disconnect(
        elapsed=5.0,
        stale_symbols=["BTCUSDT"],
    )

    manager._backfill.assert_not_awaited()
    assert manager._last_short_disconnect_s == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_recovery_backfill_triggers_after_short_disconnect_when_cache_missing() -> None:
    manager = FuturesWSManager(rest_client=SimpleNamespace(), config=WSConfig())
    manager._backfill = AsyncMock()
    manager._klines = {}

    await manager._maybe_backfill_after_disconnect(
        elapsed=5.0,
        stale_symbols=["BTCUSDT"],
    )

    manager._backfill.assert_awaited_once_with(["BTCUSDT"])
    assert manager._last_short_disconnect_s == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_reconnect_callback_fires_only_after_first_connect() -> None:
    reconnect_cb = AsyncMock()
    manager = SimpleNamespace(
        _ws_conns={"market": None},
        _connected_urls={"market": None},
        _connected_at_by_endpoint={"market": 0.0},
        _ws_conn=None,
        _last_message_ts_by_endpoint={"market": 0.0},
        _last_message_ts=0.0,
        _last_event_lag_ms=None,
        _connected_endpoints={"market": SimpleNamespace(set=lambda: None, clear=lambda: None)},
        _refresh_connected_event=lambda: None,
        _connect_counts={"market": 0},
        _connect_count=0,
        _reconnect_cb=reconnect_cb,
        _intended_streams_by_endpoint={"market": {"btcusdt@kline_15m"}},
        _last_reconnect_reason="not_started",
        _last_reconnect_reason_by_endpoint={"market": "not_started"},
    )
    ws = object()

    with patch("bot.websocket.connection.apply_tcp_keepalive"):
        from bot.websocket import connection as ws_connection

        ws_connection.apply_connected_state(manager, endpoint="market", ws=ws, url="wss://x/stream")
        assert reconnect_cb.await_count == 0

        ws_connection.apply_connected_state(manager, endpoint="market", ws=ws, url="wss://x/stream")
        await asyncio.sleep(0)

    reconnect_cb.assert_awaited_once()


@pytest.mark.asyncio
async def test_monitor_connection_silence_triggers_close_when_streams_intended() -> None:
    from bot.websocket import health as ws_health

    close = AsyncMock()
    ws = SimpleNamespace(close=close)
    manager = SimpleNamespace(
        _cfg=SimpleNamespace(health_check_silence_seconds=1.0),
        _last_message_ts_by_endpoint={"market": 10.0},
        _intended_streams_by_endpoint={"market": {"btcusdt@kline_15m"}},
    )

    with patch("bot.websocket.health.time.monotonic", return_value=20.5):
        triggered = await ws_health.monitor_connection_silence(manager, ws, "market")

    assert triggered is True
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_endpoint_loop_resets_delay_and_backfills_on_proactive_reconnect() -> None:
    from bot.websocket import connection as ws_connection

    manager = SimpleNamespace(
        _cfg=SimpleNamespace(reconnect_max_delay_seconds=16, endpoint_base_url=lambda _e: "wss://fstream.binance.com"),
        _running=True,
        _short_lived_streak=5,
        _intended_streams_by_endpoint={"market": {"btcusdt@kline_15m"}},
    )
    manager._stale_symbols = lambda: ["BTCUSDT"]
    manager._maybe_backfill_after_disconnect = AsyncMock()

    calls = {"n": 0}

    async def fake_run_stream_session(*args, **kwargs):
        calls["n"] += 1
        manager._running = False
        return True, True

    with patch("bot.websocket.connection.run_stream_session", side_effect=fake_run_stream_session):
        await ws_connection.run_endpoint_loop(
            manager,
            endpoint="market",
            backoff_reset_after_seconds=30.0,
            proactive_reconnect_after_seconds=999.0,
            parse_message=lambda raw: raw,
            market_endpoint="market",
            stale_symbols=manager._stale_symbols,
            maybe_backfill_after_disconnect=manager._maybe_backfill_after_disconnect,
            compute_disconnect_delay=lambda *a, **k: 1.0,
            connection_exceptions=(ConnectionError,),
        )

    assert calls["n"] == 1
    assert manager._short_lived_streak == 0
    manager._maybe_backfill_after_disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_endpoint_loop_reconnects_and_triggers_stale_recovery_on_disconnect() -> None:
    from bot.websocket import connection as ws_connection

    manager = SimpleNamespace(
        _cfg=SimpleNamespace(reconnect_max_delay_seconds=16, endpoint_base_url=lambda _e: "wss://fstream.binance.com"),
        _running=True,
        _short_lived_streak=0,
        _intended_streams_by_endpoint={"market": {"btcusdt@kline_15m"}},
    )
    manager._stale_symbols = lambda: ["BTCUSDT"]
    manager._maybe_backfill_after_disconnect = AsyncMock()

    async def fail_once(*args, **kwargs):
        raise ConnectionError("boom")

    with (
        patch("bot.websocket.connection.run_stream_session", side_effect=fail_once),
        patch("bot.websocket.connection.clear_endpoint_connection_state") as clear_state,
        patch("bot.websocket.connection.asyncio.sleep", new=AsyncMock(side_effect=lambda *_a, **_k: setattr(manager, "_running", False))),
    ):
        await ws_connection.run_endpoint_loop(
            manager,
            endpoint="market",
            backoff_reset_after_seconds=30.0,
            proactive_reconnect_after_seconds=999.0,
            parse_message=lambda raw: raw,
            market_endpoint="market",
            stale_symbols=manager._stale_symbols,
            maybe_backfill_after_disconnect=manager._maybe_backfill_after_disconnect,
            compute_disconnect_delay=lambda *a, **k: 0.25,
            connection_exceptions=(ConnectionError,),
        )

    clear_state.assert_called_once_with(manager, "market")
    manager._maybe_backfill_after_disconnect.assert_awaited_once()

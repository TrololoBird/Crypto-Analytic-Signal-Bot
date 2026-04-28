from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

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

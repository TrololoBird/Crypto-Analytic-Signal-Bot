"""Live smoke: Binance USD-M public REST + WebSocket (no private endpoints)."""

from __future__ import annotations


import pytest

from scripts.live_check_binance_api import _run


@pytest.mark.live
@pytest.mark.asyncio
async def test_binance_public_rest_and_ws_reconnect() -> None:
    """REST exchangeInfo/ticker/klines + WS warmup + forced reconnect."""
    await _run(
        symbols=["BTCUSDT", "ETHUSDT"],
        warmup_seconds=30.0,
        reconnect_wait_seconds=30.0,
    )

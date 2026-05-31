"""Live smoke: feature pipeline against real Binance klines."""

from __future__ import annotations

import pytest

from scripts.live_check_indicators import _run


@pytest.mark.live
@pytest.mark.asyncio
async def test_binance_klines_feature_prepare() -> None:
    """Fetch live klines and run prepare_symbol for pinned symbols."""
    await _run(symbols=["BTCUSDT", "SOLUSDT"], concurrency=2)

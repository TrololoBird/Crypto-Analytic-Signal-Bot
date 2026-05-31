"""Live smoke: WS + REST enrichments for pinned symbols."""

from __future__ import annotations

import pytest

from scripts.live_check_enrichments import _run


@pytest.mark.live
@pytest.mark.asyncio
async def test_binance_ws_enrichments_populated() -> None:
    await _run(
        symbols=["BTCUSDT", "ETHUSDT"],
        warmup_seconds=45.0,
        include_premium_stats=False,
        require_depth=False,
    )

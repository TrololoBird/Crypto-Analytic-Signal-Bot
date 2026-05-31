"""Live smoke: end-to-end pipeline (prepare → engine → delivery gates)."""

from __future__ import annotations

import pytest

from scripts.live_check_pipeline import _run


@pytest.mark.live
@pytest.mark.asyncio
async def test_binance_pipeline_smoke() -> None:
    await _run(
        symbols=["BTCUSDT"],
        limit=1,
        concurrency=1,
        warm_context=False,
        include_basis=False,
    )

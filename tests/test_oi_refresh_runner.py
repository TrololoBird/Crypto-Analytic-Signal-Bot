from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.application.oi_refresh_runner import OIRefreshRunner
from bot.market_data import BinanceFuturesMarketData


@pytest.mark.asyncio
async def test_refresh_once_can_bound_symbols_and_skip_funding_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BinanceFuturesMarketData()
    calls: list[tuple[str, str]] = []

    async def _record(stage: str, symbol: str, **_kwargs) -> float:
        calls.append((stage, symbol))
        return 1.0

    monkeypatch.setattr(
        client,
        "fetch_open_interest",
        lambda symbol, **kwargs: _record("oi_current", symbol, **kwargs),
    )
    monkeypatch.setattr(
        client,
        "fetch_open_interest_change",
        lambda symbol, **kwargs: _record("oi_change", symbol, **kwargs),
    )
    monkeypatch.setattr(
        client,
        "fetch_long_short_ratio",
        lambda symbol, **kwargs: _record("top_account_ls", symbol, **kwargs),
    )
    monkeypatch.setattr(
        client,
        "fetch_top_position_ls_ratio",
        lambda symbol, **kwargs: _record("top_position_ls", symbol, **kwargs),
    )
    monkeypatch.setattr(
        client,
        "fetch_taker_ratio",
        lambda symbol, **kwargs: _record("taker_ratio", symbol, **kwargs),
    )
    monkeypatch.setattr(
        client,
        "fetch_global_ls_ratio",
        lambda symbol, **kwargs: _record("global_ls", symbol, **kwargs),
    )
    funding = AsyncMock(return_value=[])
    monkeypatch.setattr(client, "fetch_funding_rate_history", funding)

    bot = SimpleNamespace(
        client=client,
        settings=SimpleNamespace(
            runtime=SimpleNamespace(
                startup_batch_size=3,
                startup_batch_delay_seconds=0.5,
                max_concurrent_rest_requests=3,
            )
        ),
        _update_memory_market_context=AsyncMock(),
    )
    runner = OIRefreshRunner(bot)
    shortlist = [SimpleNamespace(symbol=f"SYM{idx}USDT") for idx in range(5)]

    processed = await runner.refresh_once(
        shortlist,
        max_age_seconds=0.0,
        symbol_limit=2,
        include_funding_history=False,
        per_symbol_timeout_seconds=1.0,
    )

    assert processed == 2
    assert {symbol for _, symbol in calls} == {"SYM0USDT", "SYM1USDT"}
    assert {stage for stage, _ in calls} == {
        "oi_current",
        "oi_change",
        "top_account_ls",
        "top_position_ls",
        "taker_ratio",
        "global_ls",
    }
    funding.assert_not_awaited()

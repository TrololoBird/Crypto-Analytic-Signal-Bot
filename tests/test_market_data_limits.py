from __future__ import annotations

from bot.market_data import BinanceFuturesMarketData


def test_futures_data_limiter_is_configurable_and_clamped() -> None:
    client = BinanceFuturesMarketData(futures_data_request_limit_per_5m=42)
    assert client.state_snapshot()["futures_data_limit_per_5m"] == 42

    clamped = BinanceFuturesMarketData(futures_data_request_limit_per_5m=5000)
    assert clamped.state_snapshot()["futures_data_limit_per_5m"] == 1000

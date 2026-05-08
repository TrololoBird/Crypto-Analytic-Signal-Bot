from __future__ import annotations

import time

from bot.market_data import BinanceFuturesMarketData


def test_futures_data_limiter_is_configurable_and_clamped() -> None:
    client = BinanceFuturesMarketData(futures_data_request_limit_per_5m=42)
    assert client.state_snapshot()["futures_data_limit_per_5m"] == 42

    clamped = BinanceFuturesMarketData(futures_data_request_limit_per_5m=5000)
    assert clamped.state_snapshot()["futures_data_limit_per_5m"] == 1000


def test_public_endpoint_weight_estimates_match_usdm_docs() -> None:
    client = BinanceFuturesMarketData()

    assert client._estimate_weight("compressed_aggregate_trades_list") == 20
    assert client._estimate_weight("top_trader_long_short_ratio_accounts") == 0
    assert client._estimate_weight("top_trader_long_short_ratio_positions") == 0
    assert client._estimate_weight("global_long_short_account_ratio") == 0


def test_kline_weight_estimate_uses_limit_tiers() -> None:
    client = BinanceFuturesMarketData()

    assert client._estimate_weight("kline_candlestick_data", {"limit": 99}) == 1
    assert client._estimate_weight("kline_candlestick_data", {"limit": 300}) == 2
    assert client._estimate_weight("kline_candlestick_data", {"limit": 1000}) == 5
    assert client._estimate_weight("kline_candlestick_data", {"limit": 1500}) == 10


def test_funding_history_uses_public_request_limiter() -> None:
    client = BinanceFuturesMarketData()
    spec = client._endpoint_spec("funding_rate_history")

    assert spec.path == "/fapi/v1/fundingRate"
    assert spec.ip_limited is True
    assert client._estimate_weight("funding_rate_history") == 1


def test_public_context_cache_accessors_do_not_make_rest_calls() -> None:
    client = BinanceFuturesMarketData()
    now = time.monotonic()
    client._funding_rate_cache["BTCUSDT"] = (now, 0.0001)
    client._premium_index_all_cache = (
        now,
        {
            "BTCUSDT": {
                "funding_rate": 0.0001,
                "basis_pct": 0.02,
                "mark_price": 100.0,
                "index_price": 99.98,
            }
        },
    )

    assert client.get_cached_funding_rate("BTCUSDT") == 0.0001
    assert client.get_cached_premium_index("BTCUSDT") == {
        "funding_rate": 0.0001,
        "basis_pct": 0.02,
        "mark_price": 100.0,
        "index_price": 99.98,
    }
    assert client.get_cached_premium_index("ETHUSDT") is None

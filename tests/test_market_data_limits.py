from __future__ import annotations

import asyncio
import time

import pytest

from bot.market_data import BinanceFuturesMarketData, _SlidingWindowRateLimiter


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


@pytest.mark.asyncio
async def test_futures_data_limiter_does_not_sleep_while_holding_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = _SlidingWindowRateLimiter(max_requests=1, window_seconds=60.0)
    await limiter.acquire(label="first")

    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()
    real_sleep = asyncio.sleep

    async def fake_sleep(_delay: float) -> None:
        sleep_started.set()
        await release_sleep.wait()

    monkeypatch.setattr("bot.market_data.asyncio.sleep", fake_sleep)
    blocked = asyncio.create_task(limiter.acquire(label="second"))
    try:
        await asyncio.wait_for(sleep_started.wait(), timeout=1.0)
        await asyncio.wait_for(limiter._lock.acquire(), timeout=1.0)
        limiter._lock.release()
    finally:
        blocked.cancel()
        release_sleep.set()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        monkeypatch.setattr("bot.market_data.asyncio.sleep", real_sleep)


@pytest.mark.asyncio
async def test_http_session_uses_connector_limit_matching_rest_semaphore() -> None:
    client = BinanceFuturesMarketData()
    session = await client._get_http_session()
    try:
        assert session.connector is not None
        assert session.connector.limit == 5
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_fetch_agg_trades_caps_future_end_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BinanceFuturesMarketData()
    captured_params: list[dict] = []

    async def fake_call(_operation: str, *, params: dict, symbol: str | None = None):
        captured_params.append(dict(params))
        return []

    monkeypatch.setattr(client, "_call_public_http_json", fake_call)
    now_ms = int(time.time() * 1000)
    future_ms = now_ms + 3_600_000

    rows, complete = await client.fetch_agg_trades(
        "BTCUSDT",
        start_time_ms=now_ms - 1_000,
        end_time_ms=future_ms,
        page_limit=1,
        page_size=100,
    )

    assert rows == []
    assert complete is True
    assert captured_params[0]["endTime"] <= int(time.time() * 1000)

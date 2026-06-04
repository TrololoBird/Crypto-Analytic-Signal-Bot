"""Data capability routing tests."""

from __future__ import annotations

from types import SimpleNamespace

from bot.market.data_capability import assess_strategy_data_capability, data_pool_for_setup


def test_data_pool_for_setup() -> None:
    assert data_pool_for_setup("cvd_divergence") == "orderflow"
    assert data_pool_for_setup("funding_reversal") == "positioning"


def test_klines_pool_always_ready() -> None:
    prepared = SimpleNamespace(symbol="BTCUSDT")
    result = assess_strategy_data_capability("ema_bounce", prepared)  # type: ignore[arg-type]
    assert result.ready is True


def test_orderbook_requires_depth() -> None:
    prepared = SimpleNamespace(symbol="BTCUSDT", depth_imbalance=None)
    result = assess_strategy_data_capability("whale_walls", prepared)  # type: ignore[arg-type]
    assert result.ready is False
    assert result.reason == "data.orderbook_not_ready"

    prepared.depth_imbalance = 0.12
    result2 = assess_strategy_data_capability("whale_walls", prepared)  # type: ignore[arg-type]
    assert result2.ready is True

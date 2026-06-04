from __future__ import annotations

import polars as pl

from bot.domain.config import BotSettings, FilterConfig
from bot.domain.schemas import PreparedSymbol, UniverseSymbol
from bot.runtime.data_readiness import (
    assess_symbol_data_readiness,
    missing_derivatives_context,
)


def _settings() -> BotSettings:
    return BotSettings(
        tg_token="test",
        target_chat_id="1",
        filters=FilterConfig(min_bars_5m=30, min_bars_15m=30, min_bars_1h=30, min_bars_4h=30),
    )


def _prepared(**overrides: object) -> PreparedSymbol:
    frame = pl.DataFrame({"close": [1.0] * 300})
    base = PreparedSymbol(
        universe=UniverseSymbol(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            contract_type="PERPETUAL",
            status="TRADING",
            onboard_date_ms=0,
            quote_volume=1e9,
            price_change_pct=1.0,
            last_price=100.0,
        ),
        work_1h=frame,
        work_5m=frame,
        work_4h=frame,
        work_15m=pl.DataFrame(
            {
                "close": [1.0] * 300,
                "bid_price": [100.0] * 300,
                "ask_price": [100.1] * 300,
                "bid_qty": [10.0] * 300,
                "ask_qty": [9.0] * 300,
            }
        ),
        bid_price=100.0,
        ask_price=100.1,
        spread_bps=10.0,
        mark_price=100.05,
        oi_change_pct=0.5,
        ls_ratio=1.1,
        global_ls_ratio=1.0,
        taker_ratio=1.02,
        funding_rate=0.0001,
        funding_trend="flat",
        premium_zscore_5m=0.2,
        premium_slope_5m=0.01,
        depth_imbalance=0.05,
        microprice_bias=0.03,
        top_position_ls_ratio=1.1,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_missing_derivatives_context_treats_null_as_missing() -> None:
    enrichments = {"oi_change_pct": None, "ls_ratio": 1.1, "funding_rate": 0.0}
    missing = missing_derivatives_context(enrichments)
    assert "oi_change_pct" in missing
    assert "ls_ratio" not in missing


def test_readiness_rejects_missing_book_columns() -> None:
    settings = _settings()
    prepared = _prepared(
        work_15m=pl.DataFrame({"close": [1.0] * 300, "bid_qty": [None] * 300}),
    )
    result = assess_symbol_data_readiness(prepared, settings)
    assert result.ready is False
    assert result.reason == "data.orderbook_columns_missing"


def test_readiness_rejects_missing_derivatives_on_prepared() -> None:
    settings = _settings()
    prepared = _prepared(oi_change_pct=None)
    result = assess_symbol_data_readiness(prepared, settings)
    assert result.ready is False
    assert result.reason == "data.derivatives_context_missing"

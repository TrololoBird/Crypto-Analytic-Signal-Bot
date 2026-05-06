from __future__ import annotations

from bot.config import BotSettings, load_settings
from bot.strategies import STRATEGY_CLASSES
from bot.strategy_asset_fit import (
    ASSET_FIT_PROFILES,
    asset_fit_reject_reason,
    calculate_strategy_fit_score,
)


def test_every_registered_strategy_has_asset_fit_profile() -> None:
    setup_ids = {strategy.setup_id for strategy in STRATEGY_CLASSES}

    assert setup_ids == set(ASSET_FIT_PROFILES)
    assert all(hasattr(strategy, "asset_fit") for strategy in STRATEGY_CLASSES)


def test_asset_fit_excludes_btc_from_btc_correlation() -> None:
    context = {"symbol": "BTCUSDT", "base_asset": "BTC", "liquidity_rank": 1}

    assert (
        asset_fit_reject_reason("btc_correlation", "BTCUSDT", context)
        == "asset_fit.symbol_excluded"
    )


def test_asset_fit_excludes_eth_from_altcoin_season() -> None:
    context = {"symbol": "ETHUSDT", "base_asset": "ETH", "liquidity_rank": 2}

    assert (
        asset_fit_reject_reason("altcoin_season_index", "ETHUSDT", context)
        == "asset_fit.symbol_excluded"
    )


def test_orderbook_asset_fit_rejects_low_liquidity_alt() -> None:
    context = {"symbol": "SOMEALTUSDT", "base_asset": "SOMEALT", "liquidity_rank": 80}

    assert calculate_strategy_fit_score("SOMEALTUSDT", "whale_walls", context) == 0.0


def test_config_example_declares_priority_asset_overrides() -> None:
    settings = load_settings("config.toml.example")

    assert set(settings.assets) >= {"BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT"}
    assert settings.assets["XAUUSDT"].primary_timeframe == "1h"
    assert "funding_reversal" in settings.assets["XAUUSDT"].excluded_strategies


def test_asset_override_excludes_strategy_from_symbol() -> None:
    settings = BotSettings(
        tg_token="",
        target_chat_id="",
        assets={"XAUUSDT": {"excluded_strategies": ["funding_reversal"]}},
    )
    context = {
        "symbol": "XAUUSDT",
        "base_asset": "XAU",
        "liquidity_rank": 3,
        "funding_rate": 0.001,
        "oi_current": 1.0,
    }

    assert (
        asset_fit_reject_reason(
            "funding_reversal", "XAUUSDT", context, settings=settings
        )
        == "asset_fit.config_excluded"
    )

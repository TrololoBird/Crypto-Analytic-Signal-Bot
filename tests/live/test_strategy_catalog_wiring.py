"""Live gate: 38 strategies match STRATEGY_CATALOG wiring (no Binance call)."""

from __future__ import annotations

import pytest

from bot.delivery.tiers import classify_tier
from bot.domain.config import BotSettings, DeliveryConfig
from bot.domain.delivery_policy import R_CLASS_SETUP_IDS, r_class_blocks_action
from bot.domain.schemas import Signal
from bot.domain.strategy_catalog import (
    CATALOG_SETUP_IDS,
    PR10_WAVES,
    verify_strategy_wiring,
    wave_status,
)
from bot.strategies import STRATEGY_CLASSES


@pytest.mark.live
def test_strategy_catalog_38_registered() -> None:
    errors = verify_strategy_wiring(STRATEGY_CLASSES)
    assert not errors, "catalog wiring errors:\n" + "\n".join(errors)
    assert len(STRATEGY_CLASSES) == len(CATALOG_SETUP_IDS) == 38


@pytest.mark.live
def test_pr10_all_waves_registered() -> None:
    waves = wave_status(STRATEGY_CLASSES)
    assert all(waves.values()), f"incomplete waves: {waves}"
    assert len(PR10_WAVES) == 5


@pytest.mark.live
def test_r_class_watch_only_policy() -> None:
    assert (
        frozenset({"price_velocity", "whale_walls", "spread_strategy", "depth_imbalance"})
        == R_CLASS_SETUP_IDS
    )
    settings = BotSettings(
        tg_token="x",
        target_chat_id="1",
        delivery=DeliveryConfig(r_class_watch_only=True, action_min_score=0.5),
    )
    signal = Signal(
        symbol="BTCUSDT",
        setup_id="whale_walls",
        direction="long",
        score=0.95,
        timeframe="15m",
        entry_low=99.0,
        entry_high=101.0,
        stop=95.0,
        take_profit_1=110.0,
        take_profit_2=115.0,
        risk_reward=2.0,
    )
    assert r_class_blocks_action("whale_walls", settings)
    assert classify_tier(signal, settings).tier == "watch"
    assert classify_tier(signal, settings).reason == "r_class_watch_only"

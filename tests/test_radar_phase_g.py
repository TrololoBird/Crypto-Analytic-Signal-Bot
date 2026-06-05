"""Phase G: radar health, watch candidates, data_readiness radar_promoted."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from bot.diagnostics.config_audit import audit_filter_config
from bot.diagnostics.facade import assess_radar_store
from bot.domain.config import BotSettings, UniverseRadarConfig
from bot.domain.schemas import UniverseSymbol
from bot.market.radar_state import MarketRadarStore, SymbolRadarState, SymbolTier
from bot.market.subscription_planner import merge_order_flow_tracked_symbols
from bot.runtime.cycle_runner import CycleRunner
from bot.runtime.data_readiness import assess_symbol_data_readiness, is_radar_promoted_item
from bot.runtime.watch_escalation import collect_radar_watch_rows


def _settings() -> BotSettings:
    return BotSettings(tg_token="t", target_chat_id="1")


def test_assess_radar_store_degraded_low_count() -> None:
    store = MarketRadarStore(UniverseRadarConfig(enabled=True))
    health = assess_radar_store(store, config=store._cfg)
    assert health["attached"] is True
    assert health["status"] == "degraded"
    assert "low_symbol_count" in health["alerts"]


def test_assess_radar_store_healthy_with_ingest() -> None:
    cfg = UniverseRadarConfig(enabled=True)
    store = MarketRadarStore(cfg)
    now = time.monotonic()
    for idx in range(60):
        sym = f"SYM{idx}USDT"
        state = SymbolRadarState(symbol=sym, tier=SymbolTier.WARM, last_update_ts=now)
        state.flags = ("impulse_5m_up",)
        store._states[sym] = state
    health = assess_radar_store(store, config=cfg, now=now)
    assert health["status"] == "healthy"
    assert health["flagged_count"] == 60


def test_data_readiness_radar_promoted_skips_derivatives() -> None:
    settings = _settings()
    item = UniverseSymbol(
        symbol="TESTUSDT",
        base_asset="TEST",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=1e8,
        price_change_pct=1.0,
        last_price=1.0,
        shortlist_bucket="radar",
        shortlist_reasons=("radar_promoted",),
    )
    prepared = MagicMock()
    prepared.symbol = "TESTUSDT"
    prepared.mark_price = 1.0
    prepared.spread_bps = 5.0
    prepared.work_5m = MagicMock(height=300, is_empty=False)
    prepared.work_15m = MagicMock(height=300, is_empty=False)
    prepared.work_1h = MagicMock(height=300, is_empty=False)
    prepared.work_4h = MagicMock(height=300, is_empty=False)
    prepared.work_15m.is_empty = False
    prepared.work_15m.columns = []
    prepared.oi_change_pct = None
    prepared.ls_ratio = None
    prepared.global_ls_ratio = None
    prepared.taker_ratio = None
    prepared.funding_rate = None

    result = assess_symbol_data_readiness(
        prepared,
        settings,
        universe_item=item,
    )
    assert result.ready is True
    assert result.details.get("strict_derivatives_skipped") is True


def test_collect_radar_watch_rows_excludes_shortlist() -> None:
    from bot.domain.config import UniverseRadarConfig

    cfg = UniverseRadarConfig(enabled=True)
    store = MarketRadarStore(cfg)
    store._states["HOTUSDT"] = SymbolRadarState(
        symbol="HOTUSDT",
        tier=SymbolTier.HOT,
        flags=("impulse_5m_up",),
        last_update_ts=time.monotonic(),
    )
    bot = MagicMock()
    bot.settings.universe.radar = cfg
    bot._shortlist = [
        UniverseSymbol(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            contract_type="PERPETUAL",
            status="TRADING",
            onboard_date_ms=0,
            quote_volume=1e9,
            price_change_pct=1.0,
            last_price=1.0,
        )
    ]
    rows = collect_radar_watch_rows(bot, store)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "HOTUSDT"


def test_merge_priority_before_shortlist() -> None:
    merged = merge_order_flow_tracked_symbols(
        ["aaa", "bbb"],
        priority_symbols=["hot"],
    )
    assert merged[0] == "hot"


def test_is_radar_promoted_item_by_bucket() -> None:
    item = UniverseSymbol(
        symbol="XUSDT",
        base_asset="X",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=1e7,
        price_change_pct=1.0,
        last_price=1.0,
        shortlist_bucket="radar",
    )
    assert is_radar_promoted_item(item) is True
    assert is_radar_promoted_item(None) is False


@pytest.mark.asyncio
async def test_emergency_shortlist_defers_recent_radar() -> None:
    import asyncio

    settings = _settings()
    settings.runtime.emergency_fallback_seconds = 30
    bot = MagicMock()
    bot.settings = settings
    now = asyncio.get_running_loop().time()
    bot._last_emergency_radar_scan = {"RADUSDT": now}
    radar_item = UniverseSymbol(
        symbol="RADUSDT",
        base_asset="RAD",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=1e8,
        price_change_pct=1.0,
        last_price=1.0,
        shortlist_bucket="radar",
    )
    core_item = UniverseSymbol(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=1e9,
        price_change_pct=1.0,
        last_price=1.0,
    )
    runner = CycleRunner(bot)
    filtered = runner._emergency_shortlist_for_scan([core_item, radar_item])
    assert len(filtered) == 1
    assert filtered[0].symbol == "BTCUSDT"


def test_audit_filter_stages_unknown_warns() -> None:
    settings = _settings()
    settings.filters.enabled_stages = ("freshness", "bogus_stage")
    warnings = audit_filter_config(settings)
    assert any("unknown stage" in w for w in warnings)

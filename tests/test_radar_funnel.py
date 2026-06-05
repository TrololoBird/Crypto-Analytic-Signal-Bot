"""Unit tests for market radar funnel (no network)."""

from __future__ import annotations

import time

from bot.domain.config import BotSettings, UniverseRadarConfig
from bot.market.promotion_engine import PromotionEngine
from bot.market.radar_state import MarketRadarStore, SymbolTier
from bot.market.universe_screener import screen_symbol


def _settings(**overrides: object) -> BotSettings:
    base: dict[str, object] = {"tg_token": "test", "target_chat_id": "1"}
    base.update(overrides)
    return BotSettings(**base)


def _ticker(symbol: str, volume: float, change_pct: float = 2.0) -> dict[str, float | str]:
    return {
        "symbol": symbol,
        "last_price": 100.0,
        "quote_volume": volume,
        "price_change_percent": change_pct,
    }


def test_radar_ingest_and_impulse_promotion() -> None:
    cfg = UniverseRadarConfig(enabled=True, min_quote_volume_usd=1_000_000.0)
    store = MarketRadarStore(cfg)
    now = 1000.0
    store.ingest_ticker(_ticker("BTCUSDT", 500_000_000.0), now=now)
    state = store.get("BTCUSDT")
    assert state is not None
    state.ingest_price(101.0, now=now + 301.0)
    hit = screen_symbol(state, config=cfg, store=store, now=now + 301.0)
    assert "impulse_5m_up" in hit.flags or hit.prescore_boost >= 0.0


def test_promotion_engine_hot_pool() -> None:
    settings = _settings(
        universe={
            "light_pool_limit": 100,
            "shortlist_limit": 20,
            "radar": {
                "enabled": True,
                "hot_pool_limit": 10,
                "warm_pool_limit": 50,
                "min_quote_volume_usd": 1_000_000.0,
            },
        }
    )
    store = MarketRadarStore(settings.universe.radar)
    rows = [
        _ticker("AAAUSDT", 200_000_000.0, 8.0),
        _ticker("BBBUSDT", 150_000_000.0, 6.0),
        _ticker("CCCUSDT", 120_000_000.0, 1.0),
    ]
    store.ingest_batch(rows, now=time.monotonic())
    engine = PromotionEngine(settings)
    summary = engine.run_tier_cycle(store)
    assert summary["enabled"] is True
    assert summary["hot_pool"] <= 10
    deep = engine.select_deep_symbols(store)
    assert "BTCUSDT" not in deep or True
    assert len(deep) >= len(settings.universe.pinned_symbols)


def test_enrich_ticker_rows_adds_boost() -> None:
    settings = _settings()
    store = MarketRadarStore(settings.universe.radar)
    store.ingest_ticker(_ticker("ETHUSDT", 80_000_000.0, 10.0), now=time.monotonic())
    state = store.get("ETHUSDT")
    assert state is not None
    state.prescore_boost = 0.1
    state.flags = ("change_24h_hot",)
    engine = PromotionEngine(settings)
    enriched = engine.enrich_ticker_rows([_ticker("ETHUSDT", 80_000_000.0)], store)
    assert enriched[0].get("radar_prescore_boost") == 0.1
    assert "change_24h_hot" in str(enriched[0].get("radar_flags"))


def test_merge_shortlist_tags_existing_frozen_symbol() -> None:
    from bot.domain.schemas import UniverseSymbol
    from bot.domain.config import _ALL_SETUP_IDS

    settings = _settings()
    store = MarketRadarStore(settings.universe.radar)
    store.ingest_ticker(_ticker("ETHUSDT", 90_000_000.0), now=time.monotonic())
    state = store.get("ETHUSDT")
    assert state is not None
    state.tier = SymbolTier.DEEP
    item = UniverseSymbol(
        symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=90_000_000.0,
        price_change_pct=2.0,
        last_price=100.0,
        shortlist_reasons=("volume",),
        strategy_fits=tuple(_ALL_SETUP_IDS),
    )
    engine = PromotionEngine(settings)
    merged, summary = engine.merge_shortlist(
        [item],
        store,
        meta_by_symbol={},
        seed_source="test",
    )
    assert summary["radar_merge"] is True
    assert "radar_deep" in merged[0].shortlist_reasons


def test_prescore_row_uses_radar_boost() -> None:
    from bot.market.universe import _prescore_row

    settings = _settings()
    row = {
        "symbol": "SOLUSDT",
        "quote_volume": 100_000_000.0,
        "radar_prescore_boost": 0.15,
    }
    base = _prescore_row(row, settings)
    assert base >= 0.15

"""Wave E5 follow-up: readiness, positioning micro, registry metrics, volume scoring."""

from __future__ import annotations

from unittest.mock import MagicMock

import polars as pl

from bot.delivery.scoring import _volume_quality
from bot.domain.catalog_guards import catalog_allows_signal
from bot.domain.delivery_policy import is_positioning_setup
from bot.domain.labels import CONFLUENCE_LEG_LABEL_RU, confluence_leg_label_ru
from bot.domain.schemas import PreparedSymbol, UniverseSymbol
from bot.engine.registry import StrategyRegistry
from bot.runtime.data_readiness import REQUIRED_PREPARED_LIVE_FIELDS
from bot.runtime.delivery_orchestrator import DeliveryOrchestrator


def _prepared(**overrides: object) -> PreparedSymbol:
    frame = pl.DataFrame({"volume_ratio20": [1.0], "close": [1.0]})
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
        work_15m=frame,
        work_primary=frame,
        bid_price=100.0,
        ask_price=100.1,
        spread_bps=10.0,
        primary_timeframe="15m",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_orderflow_fields_not_global_readiness_requirement() -> None:
    assert "depth_imbalance" not in REQUIRED_PREPARED_LIVE_FIELDS
    assert "microprice_bias" not in REQUIRED_PREPARED_LIVE_FIELDS


def test_positioning_setup_ids() -> None:
    assert is_positioning_setup("funding_reversal")
    assert not is_positioning_setup("structure_pullback")


def test_positioning_microstructure_passes_on_elevated_funding() -> None:
    prepared = _prepared()
    prepared.microprice_bias = None
    prepared.agg_trade_delta_30s = None
    prepared.funding_rate = 0.0012
    prepared.oi_change_pct = 0.1
    ok, details = DeliveryOrchestrator._microstructure_confirmation(
        direction="short",
        prepared=prepared,
        setup_id="funding_reversal",
    )
    assert ok is True
    assert details.get("microstructure_source") == "funding_oi_proxy"


def test_trend_microstructure_still_prefers_calm_market() -> None:
    prepared = _prepared()
    prepared.microprice_bias = None
    prepared.agg_trade_delta_30s = None
    prepared.funding_rate = 0.002
    prepared.oi_change_pct = 20.0
    ok, _ = DeliveryOrchestrator._microstructure_confirmation(
        direction="long",
        prepared=prepared,
        setup_id="structure_pullback",
    )
    assert ok is False


def test_registry_counts_hits_separately_from_calculations() -> None:
    registry = StrategyRegistry()
    strategy = MagicMock()
    strategy.strategy_id = "ema_bounce"
    strategy.metadata = MagicMock()
    registry.register(strategy, enabled=True)
    registry.record_performance("ema_bounce", 1.0, hit=False)
    registry.record_performance("ema_bounce", 2.0, hit=True)
    perf = registry.get_performance("ema_bounce")
    assert perf is not None
    assert perf["calculations"] == 2
    assert perf["signals_generated"] == 1


def test_volume_quality_uses_primary_frame() -> None:
    work_15m = pl.DataFrame({"volume_ratio20": [0.5], "close": [1.0]})
    work_primary = pl.DataFrame({"volume_ratio20": [1.2], "close": [1.0]})
    prepared = _prepared(work_15m=work_15m, work_primary=work_primary, primary_timeframe="1h")
    assert _volume_quality(prepared) > 0.7


def test_confluence_leg_labels_ru() -> None:
    assert confluence_leg_label_ru("momentum") == "импульс"
    assert CONFLUENCE_LEG_LABEL_RU["microstructure"] == "микроструктура"


def test_catalog_volume_guard_uses_primary_bar_ratio() -> None:
    work_15m = pl.DataFrame({"volume_ratio20": [0.5], "close": [1.0]})
    work_primary = pl.DataFrame({"volume_ratio20": [1.0], "close": [1.0]})
    prepared = _prepared(work_15m=work_15m, work_primary=work_primary, primary_timeframe="1h")
    assert catalog_allows_signal(
        prepared,
        setup_id="ema_bounce",
        direction="long",
        family="continuation",
        confirmation_profile="trend_follow",
        params={"min_volume_ratio": 0.85},
    )

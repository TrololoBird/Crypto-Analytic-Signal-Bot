"""Wave F10 / Agent K: catalog config schema, MTF guards, kline coverage, assets, feature snapshot."""

from __future__ import annotations

from unittest.mock import MagicMock

import polars as pl
import pytest

from bot.domain.catalog_guards import catalog_allows_signal
from bot.domain.config import BotSettings
from bot.domain.contracts import (
    PUBLIC_FEATURE_FIELDS,
    build_public_feature_snapshot,
    validate_public_feature_payload,
)
from bot.domain.schemas import PreparedSymbol, UniverseSymbol
from bot.domain.strategy_catalog import (
    CATALOG_SETUP_PARAM_KEYS,
    catalog_required_timeframes_for_enabled,
    verify_config_setup_references,
)
from bot.persistence.outcomes import build_prepared_feature_snapshot, extract_features_from_signal


def _minimal_settings(**overrides: object) -> BotSettings:
    payload: dict[str, object] = {
        "tg_token": "123456789012345678901234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "target_chat_id": "-1001234567890",
    }
    payload.update(overrides)
    return BotSettings.model_validate(payload)


def _prepared(**overrides: object) -> PreparedSymbol:
    frame = pl.DataFrame(
        {
            "volume_ratio20": [1.2],
            "close": [100.0],
            "ema20": [101.0],
            "ema50": [100.0],
            "ema200": [99.0],
            "rsi14": [55.0],
            "adx14": [25.0],
        }
    )
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
        work_4h=frame,
        work_5m=frame,
        work_15m=frame,
        work_primary=frame,
        bid_price=100.0,
        ask_price=100.1,
        spread_bps=10.0,
        primary_timeframe="15m",
        bias_4h="downtrend",
        bias_1h="downtrend",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_k3_skips_unknown_filters_setup_param_key() -> None:
    settings = _minimal_settings(
        filters={"setups": {"ema_bounce": {"totally_unknown_param": 1.0, "base_score": 0.55}}},
    )
    params = settings.filters.setups.get("ema_bounce", {})
    assert "totally_unknown_param" not in params
    assert params.get("base_score") == 0.55


def test_k3_verify_config_flags_unknown_param_keys() -> None:
    settings = BotSettings.model_construct(
        tg_token="x",
        target_chat_id="-1",
        filters=BotSettings.model_fields["filters"].default_factory(),  # type: ignore[attr-defined]
    )
    settings.filters.setups = {"ema_bounce": {"bogus_key": 1.0}}  # type: ignore[assignment]
    errors = verify_config_setup_references(settings)
    assert any("unknown param keys" in err and "bogus_key" in err for err in errors)


def test_k3_catalog_param_schema_covers_example_keys() -> None:
    assert "base_score" in CATALOG_SETUP_PARAM_KEYS
    assert "min_rr" in CATALOG_SETUP_PARAM_KEYS
    assert len(CATALOG_SETUP_PARAM_KEYS) >= 100


def test_k5_catalog_trend_guard_uses_shared_mtf_reason_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closes = [100.0 - i * 0.5 for i in range(60)]
    bearish = pl.DataFrame(
        {
            "volume_ratio20": [1.2] * 60,
            "close": closes,
            "ema20": closes,
            "ema50": closes,
            "ema200": closes,
            "rsi14": [45.0] * 60,
            "adx14": [25.0] * 60,
        }
    )
    prepared = _prepared(work_1h=bearish, work_4h=bearish)
    reject = MagicMock()
    monkeypatch.setattr("bot.domain.catalog_guards._reject", reject)
    allowed = catalog_allows_signal(
        prepared,
        setup_id="structure_pullback",
        direction="long",
        family="continuation",
        confirmation_profile="trend_follow",
        params={"min_volume_ratio": 0.0, "min_adx_1h": 0.0},
    )
    assert allowed is False
    reason = str(reject.call_args.args[2])
    assert reason == "htf_conflict"


def test_k6_catalog_required_tfs_covered_by_ws_and_analysis_union() -> None:
    settings = _minimal_settings()
    required = catalog_required_timeframes_for_enabled(settings)
    assert "4h" in required
    available = set(settings.ws.kline_intervals) | set(settings.runtime.analysis_kline_intervals)
    rest_only = {"4h", "1d"}
    assert required - rest_only <= available
    assert verify_config_setup_references(settings) == []


def test_k6_rejects_missing_catalog_timeframe_coverage() -> None:
    settings = _minimal_settings(
        runtime={"analysis_kline_intervals": ["5m", "1h"]},
        ws={"kline_intervals": ["5m", "1h"]},
    )
    errors = verify_config_setup_references(settings)
    assert any("catalog required timeframes not covered" in err and "15m" in err for err in errors)


def test_k8_rejects_unknown_asset_strategy_lists() -> None:
    settings = _minimal_settings(
        assets={
            "BTCUSDT": {
                "excluded_strategies": ["not_a_real_setup"],
                "allowed_strategies": ["also_fake"],
            }
        }
    )
    errors = verify_config_setup_references(settings)
    assert any("excluded_strategies" in err and "not_a_real_setup" in err for err in errors)
    assert any("allowed_strategies" in err and "also_fake" in err for err in errors)


def test_k10_build_public_feature_snapshot_validates_contract() -> None:
    snapshot = build_public_feature_snapshot(None)
    validate_public_feature_payload(snapshot)
    assert set(snapshot) == set(PUBLIC_FEATURE_FIELDS)


def test_k10_outcome_path_validates_prepared_snapshot() -> None:
    snapshot = build_prepared_feature_snapshot(_prepared())
    signal = MagicMock()
    signal.score = 0.7
    signal.spread_bps = 5.0
    signal.quote_volume = 1e9
    signal.orderflow_delta_ratio = 0.1
    signal.risk_reward = 2.0
    signal.stop_distance_pct = 1.0
    signal.entry_mid = 100.0
    signal.bias_4h = "neutral"
    signal.setup_id = "ema_bounce"
    signal.direction = "long"
    signal.timeframe = "15m"
    features = extract_features_from_signal(signal, prepared_data=snapshot)
    assert features.rsi_15m == snapshot["rsi_15m"]


def test_k10_rejects_invalid_prepared_data_on_outcome_path() -> None:
    signal = MagicMock()
    signal.score = 0.7
    signal.spread_bps = 5.0
    signal.quote_volume = 1e9
    signal.orderflow_delta_ratio = 0.1
    signal.risk_reward = 2.0
    signal.stop_distance_pct = 1.0
    signal.entry_mid = 100.0
    signal.bias_4h = "neutral"
    signal.setup_id = "ema_bounce"
    signal.direction = "long"
    signal.timeframe = "15m"
    with pytest.raises(ValueError, match="schema mismatch"):
        extract_features_from_signal(signal, prepared_data={"rsi_15m": 1.0})

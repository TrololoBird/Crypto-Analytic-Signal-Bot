"""Wave E8 agent D — primary-aware scoring, history prior, data quality flags."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from bot.delivery.confluence import ConfluenceEngine, MIN_HISTORY_SAMPLES
from bot.delivery.scoring import _mtf_alignment, _structure_clarity
from bot.domain.config import BotSettings
from bot.domain.schemas import PreparedSymbol, Signal, UniverseSymbol
from bot.features.prepare_frame import (
    reset_frame_indicator_fallbacks,
    take_frame_indicator_fallbacks,
    _log_indicator_fallback,
)


def _universe() -> UniverseSymbol:
    return UniverseSymbol(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=1e9,
        price_change_pct=1.0,
        last_price=100.0,
    )


def _prepared(**overrides: object) -> PreparedSymbol:
    rows = 30
    work_1h = pl.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [100.0] * rows,
            "ema20": [98.0] * rows,
            "ema50": [99.0] * rows,
            "ema200": [100.0] * rows,
            "atr14": [1.0] * rows,
        }
    )
    base = PreparedSymbol(
        universe=_universe(),
        work_1h=work_1h,
        work_15m=work_1h,
        work_primary=work_1h,
        bid_price=100.0,
        ask_price=100.1,
        spread_bps=10.0,
        structure_1h="ranging",
        regime_4h_confirmed="uptrend",
        regime_1h_confirmed="uptrend",
        poc_1h=100.0,
        primary_timeframe="15m",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _signal(**overrides: object) -> Signal:
    now = datetime.now(UTC)
    base = {
        "symbol": "BTCUSDT",
        "setup_id": "structure_pullback",
        "direction": "long",
        "score": 0.72,
        "timeframe": "1h",
        "entry_low": 99.5,
        "entry_high": 100.5,
        "stop": 95.0,
        "take_profit_1": 110.0,
        "take_profit_2": 115.0,
        "risk_reward": 2.0,
        "created_at": now,
        "valid_until": now + timedelta(hours=12),
    }
    base.update(overrides)
    return Signal(**base)


def _uptrend_primary_frame() -> pl.DataFrame:
    rows = 30
    return pl.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [100.0] * rows,
            "ema20": [110.0] * rows,
            "ema50": [105.0] * rows,
            "ema200": [100.0] * rows,
            "atr14": [1.0] * rows,
        }
    )


def test_mtf_alignment_uses_primary_regime_when_not_15m() -> None:
    work_primary = _uptrend_primary_frame()
    prepared = _prepared(
        work_primary=work_primary,
        primary_timeframe="1h",
        structure_1h="ranging",
        regime_4h_confirmed="uptrend",
    )
    signal = _signal(direction="long")
    aligned_15m = _mtf_alignment(_prepared(structure_1h="ranging"), signal)
    aligned_1h = _mtf_alignment(prepared, signal)
    assert aligned_15m < aligned_1h
    assert aligned_1h == pytest.approx(1.0)


def test_structure_clarity_uses_primary_swings_when_not_15m() -> None:
    rows = 30
    lows = [100.0 - abs(i - 15) * 0.05 for i in range(rows)]
    highs = [low + 1.0 for low in lows]
    work_primary = pl.DataFrame(
        {
            "open": lows,
            "high": highs,
            "low": lows,
            "close": lows,
            "ema20": [100.0] * rows,
            "ema50": [100.0] * rows,
            "ema200": [100.0] * rows,
            "atr14": [0.5] * rows,
        }
    )
    flat_1h = pl.DataFrame(
        {
            "open": [200.0] * rows,
            "high": [201.0] * rows,
            "low": [199.0] * rows,
            "close": [200.0] * rows,
            "ema20": [200.0] * rows,
            "ema50": [200.0] * rows,
            "ema200": [200.0] * rows,
            "atr14": [5.0] * rows,
        }
    )
    signal = _signal(entry_low=99.8, entry_high=100.2, direction="long")
    clarity_primary = _structure_clarity(
        _prepared(
            work_1h=flat_1h,
            work_primary=work_primary,
            primary_timeframe="1h",
            poc_1h=100.0,
        ),
        signal,
    )
    clarity_15m = _structure_clarity(
        _prepared(work_1h=flat_1h, work_primary=flat_1h, primary_timeframe="15m"),
        signal,
    )
    assert clarity_primary > clarity_15m


def test_resolve_history_count_from_repository_when_tracking_ref_present() -> None:
    repo = MagicMock()
    repo.setup_history_count.return_value = MIN_HISTORY_SAMPLES + 5
    engine = ConfluenceEngine(BotSettings(tg_token="test", target_chat_id="1"), repository=repo)
    signal = _signal()
    assert engine._resolve_history_count(signal) == MIN_HISTORY_SAMPLES + 5
    repo.setup_history_count.assert_called_once_with("structure_pullback")


def test_calibrated_prior_uses_repository_history_count() -> None:
    repo = MagicMock()
    repo.setup_history_count.return_value = MIN_HISTORY_SAMPLES
    engine = ConfluenceEngine(BotSettings(tg_token="test", target_chat_id="1"), repository=repo)
    calibrated = engine._calibrate_setup_prior(0.80, history_count=engine._resolve_history_count(_signal()))
    assert calibrated > 0.5


def test_indicator_fallback_flags_roundtrip() -> None:
    reset_frame_indicator_fallbacks()
    _log_indicator_fallback("ema_polars_ta", RuntimeError("test"))
    assert take_frame_indicator_fallbacks() == ["indicator_fallback:ema_polars_ta"]
    assert take_frame_indicator_fallbacks() == []


def test_prepare_symbol_sets_data_quality_flags() -> None:
    from bot.domain.schemas import SymbolFrames
    from bot.features.prepare import prepare_symbol

    rows = 240
    frame = pl.DataFrame(
        {
            "open_time": list(range(rows)),
            "close_time": list(range(rows)),
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [100.0] * rows,
            "volume": [1000.0] * rows,
            "quote_volume": [1000.0] * rows,
            "num_trades": [100] * rows,
            "taker_buy_base_volume": [500.0] * rows,
            "taker_buy_quote_volume": [500.0] * rows,
        }
    )
    frames = SymbolFrames(
        symbol="BTCUSDT",
        df_1h=frame,
        df_15m=frame,
        df_5m=frame,
        df_4h=frame,
        bid_price=100.0,
        ask_price=100.1,
        bid_qty=10.0,
        ask_qty=9.0,
    )
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
    )
    with patch(
        "bot.features.prepare.take_frame_indicator_fallbacks",
        side_effect=[["indicator_fallback:obv"], [], [], []],
    ):
        prepared = prepare_symbol(
            _universe(),
            frames,
            minimums={"1h": 30, "15m": 30, "5m": 30, "4h": 30},
            settings=settings,
        )
    assert prepared is not None
    assert "indicator_fallback:obv" in prepared.data_quality_flags


def test_weight_redistribution_note_when_crowding_missing() -> None:
    prepared = _prepared(
        data_freshness_flags=("crowding_context_missing",),
        funding_rate=0.0001,
        oi_change_pct=0.05,
        ls_ratio=None,
        top_account_ls_ratio=None,
        top_position_ls_ratio=None,
        global_ls_ratio=None,
        global_account_ls_ratio=None,
        top_vs_global_ls_gap=None,
        taker_ratio=None,
    )
    work_15m = pl.DataFrame({"volume_ratio20": [1.2], "close": [100.0], "delta_ratio": [0.6]})
    prepared.work_15m = work_15m
    engine = ConfluenceEngine(BotSettings(tg_token="test", target_chat_id="1"))
    result = engine.score(_signal(), prepared)
    note = result.notes.get("weight_redistribution")
    assert note is not None
    assert note["reason"] == "crowding_context_missing"
    assert "crowd_position" in note["excluded_components"]
    assert note["redistributed_to"]
    payload = result.to_scoring_result().to_dict()
    assert payload["notes"]["weight_redistribution"]["reason"] == "crowding_context_missing"

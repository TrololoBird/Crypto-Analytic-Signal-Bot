from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from bot.config import BotSettings
from bot.models import PreparedSymbol, UniverseSymbol
from bot.strategies import STRATEGY_CLASSES
from bot.strategies.bos_choch import (
    _select_external_stop_level,
    _select_stop_level_with_fallback,
)
from bot.strategies.keltner_breakout import KeltnerBreakoutSetup
from bot.strategies.price_velocity import PriceVelocitySetup
from bot.strategies.session_killzone import SessionKillzoneSetup, _active_killzone_name
from bot.strategies.supertrend_follow import SuperTrendFollowSetup
from bot.strategies.volume_anomaly import VolumeAnomalySetup
from bot.strategies.volume_climax_reversal import VolumeClimaxReversalSetup
from bot.strategies.vwap_trend import VWAPTrendSetup


def _prepared_symbol() -> PreparedSymbol:
    now = datetime.now(UTC)
    frame = pl.DataFrame(
        {
            "open_time": [now],
            "close_time": [now],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1000.0],
            "ema20": [100.0],
            "ema50": [100.0],
            "ema200": [100.0],
            "atr14": [1.0],
            "adx14": [30.0],
            "delta_ratio": [0.5],
            "volume_ratio20": [1.0],
        }
    )
    universe = UniverseSymbol(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=1_000_000,
        price_change_pct=0.0,
        last_price=100.0,
    )
    return PreparedSymbol(
        universe=universe,
        work_1h=frame,
        work_15m=frame,
        bid_price=99.9,
        ask_price=100.1,
        spread_bps=5.0,
    )


def _strategy_frame(
    rows: int = 40,
    *,
    last_open: float = 101.0,
    last_close: float = 102.2,
    prev_close: float = 100.9,
    last_vwap: float = 101.0,
    prev_vwap: float = 101.0,
    last_volume_ratio: float = 1.4,
    last_close_position: float = 0.8,
    last_rsi: float = 56.0,
    last_adx: float = 25.0,
    last_supertrend_dir: float = 1.0,
    last_roc10: float = 1.1,
    last_kc_upper: float = 101.5,
    last_kc_lower: float = 99.0,
    last_prev_donchian_low: float = 99.0,
    last_prev_donchian_high: float = 103.0,
) -> pl.DataFrame:
    now = datetime.now(UTC)
    close = [100.0 + i * 0.05 for i in range(rows)]
    close[-2] = prev_close
    close[-1] = last_close
    open_ = [value - 0.2 for value in close]
    open_[-1] = last_open
    high = [value + 0.8 for value in close]
    low = [value - 0.8 for value in close]
    vwap = [100.5 + i * 0.02 for i in range(rows)]
    vwap[-2] = prev_vwap
    vwap[-1] = last_vwap
    volume_ratio = [1.1 for _ in range(rows)]
    volume_ratio[-1] = last_volume_ratio
    close_position = [0.55 for _ in range(rows)]
    close_position[-1] = last_close_position
    rsi = [52.0 for _ in range(rows)]
    rsi[-1] = last_rsi
    return pl.DataFrame(
        {
            "open_time": [now for _ in range(rows)],
            "close_time": [now for _ in range(rows)],
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": [1000.0 + i for i in range(rows)],
            "ema20": [101.0 for _ in range(rows)],
            "ema50": [100.5 for _ in range(rows)],
            "ema200": [99.0 for _ in range(rows)],
            "vwap": vwap,
            "atr14": [1.0 for _ in range(rows)],
            "atr_pct": [1.0 for _ in range(rows)],
            "adx14": [last_adx for _ in range(rows)],
            "delta_ratio": [0.55 for _ in range(rows)],
            "volume_ratio20": volume_ratio,
            "close_position": close_position,
            "rsi14": rsi,
            "supertrend_dir": [last_supertrend_dir for _ in range(rows)],
            "roc10": [last_roc10 for _ in range(rows)],
            "kc_upper": [last_kc_upper for _ in range(rows)],
            "kc_lower": [last_kc_lower for _ in range(rows)],
            "prev_donchian_low20": [last_prev_donchian_low for _ in range(rows)],
            "prev_donchian_high20": [last_prev_donchian_high for _ in range(rows)],
        }
    )


def _prepared_with_frames(
    frame_15m: pl.DataFrame, frame_1h: pl.DataFrame
) -> PreparedSymbol:
    universe = UniverseSymbol(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=10_000_000,
        price_change_pct=0.0,
        last_price=float(frame_15m.item(-1, "close")),
    )
    return PreparedSymbol(
        universe=universe,
        work_1h=frame_1h,
        work_15m=frame_15m,
        bid_price=101.9,
        ask_price=102.0,
        spread_bps=1.0,
        work_5m=frame_15m,
        work_4h=frame_1h,
        bias_4h="uptrend",
        bias_1h="uptrend",
        regime_1h_confirmed="uptrend",
        regime_4h_confirmed="uptrend",
        structure_1h="uptrend",
    )


@pytest.mark.parametrize("strategy_cls", STRATEGY_CLASSES)
def test_strategy_metadata_and_calculate_contract(strategy_cls: type) -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    strategy = strategy_cls(settings=settings)
    result = strategy.calculate(_prepared_symbol())

    assert strategy.metadata.strategy_id
    assert isinstance(strategy.get_optimizable_params(settings), dict)
    assert result.setup_id == strategy.metadata.strategy_id


def test_session_killzone_rejects_empty_15m_before_time_lookup() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    prepared = _prepared_symbol()
    prepared.work_15m = pl.DataFrame()

    result = SessionKillzoneSetup(settings=settings).calculate(prepared)

    assert result.signal is None
    assert result.decision.reason_code.endswith("insufficient_15m_bars")


def test_session_killzone_overlap_is_first_class_window() -> None:
    assert _active_killzone_name(14) == "Overlap"
    assert _active_killzone_name(8) == "London"
    assert _active_killzone_name(1) == "Asia"
    assert _active_killzone_name(23) is None


def test_session_killzone_supports_configured_cross_midnight_window() -> None:
    params = {
        "overlap_start_hour_utc": 22,
        "overlap_end_hour_utc": 2,
        "london_start_hour_utc": 7,
        "london_end_hour_utc": 10,
        "ny_start_hour_utc": 13,
        "ny_end_hour_utc": 16,
        "asia_start_hour_utc": 0,
        "asia_end_hour_utc": 0,
    }

    assert _active_killzone_name(23, params) == "Overlap"
    assert _active_killzone_name(1, params) == "Overlap"
    assert _active_killzone_name(3, params) is None


def test_bos_choch_external_stop_selector_reports_side_filtering() -> None:
    level, details = _select_external_stop_level(
        markers=[1.0, 1.0, -1.0],
        levels=[99.0, 101.0, 98.0],
        search_end=2,
        marker=1.0,
        price=100.0,
        above_price=True,
    )

    assert level == 101.0
    assert details["external_marker_candidates"] == 1
    assert details["external_side_filtered"] == 0
    assert details["external_selected_index"] == 1


def test_bos_choch_external_stop_selector_diagnoses_missing_anchor() -> None:
    level, details = _select_external_stop_level(
        markers=[1.0, 1.0],
        levels=[98.0, 99.0],
        search_end=1,
        marker=1.0,
        price=100.0,
        above_price=True,
    )

    assert level is None
    assert details["external_marker_candidates"] == 2
    assert details["external_side_filtered"] == 2


def test_bos_choch_stop_selector_falls_back_to_internal_anchor() -> None:
    level, source, details = _select_stop_level_with_fallback(
        frame=pl.DataFrame(),
        external_markers=[1.0, 1.0],
        external_levels=[98.0, 99.0],
        internal_markers=[-1.0, 1.0],
        internal_levels=[97.0, 101.0],
        search_end=1,
        marker=1.0,
        price=100.0,
        break_level=100.0,
        atr=0.0,
        above_price=True,
    )

    assert level == pytest.approx(101.0)
    assert source == "internal_swing"
    assert details["fallback_used"] == "internal_swing_stop"
    assert details["external_side_filtered"] == 2
    assert details["internal_selected_index"] == 1


def test_vwap_trend_detects_long_reclaim() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    frame_15m = _strategy_frame(
        last_open=101.4,
        last_close=102.2,
        prev_close=100.9,
        last_vwap=101.0,
        prev_vwap=101.0,
        last_volume_ratio=1.35,
    )
    prepared = _prepared_with_frames(frame_15m, _strategy_frame())

    result = VWAPTrendSetup(settings=settings).calculate(prepared)

    assert result.signal is not None
    assert result.signal.setup_id == "vwap_trend"
    assert result.signal.direction == "long"


def test_supertrend_follow_detects_long_pullback() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    frame_15m = _strategy_frame(
        last_open=101.4,
        last_close=102.1,
        last_volume_ratio=1.25,
        last_supertrend_dir=1.0,
    )
    prepared = _prepared_with_frames(
        frame_15m, _strategy_frame(last_supertrend_dir=1.0)
    )

    result = SuperTrendFollowSetup(settings=settings).calculate(prepared)

    assert result.signal is not None
    assert result.signal.setup_id == "supertrend_follow"
    assert result.signal.direction == "long"


def test_price_velocity_detects_long_impulse() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    frame_15m = _strategy_frame(
        last_open=100.8,
        last_close=102.4,
        last_volume_ratio=1.8,
        last_close_position=0.82,
        last_rsi=64.0,
        last_roc10=1.4,
    )
    prepared = _prepared_with_frames(frame_15m, _strategy_frame())

    result = PriceVelocitySetup(settings=settings).calculate(prepared)

    assert result.signal is not None
    assert result.signal.setup_id == "price_velocity"
    assert result.signal.direction == "long"


def test_volume_anomaly_detects_long_impulse() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    frame_15m = _strategy_frame(
        last_open=100.8,
        last_close=102.4,
        last_volume_ratio=2.6,
        last_close_position=0.86,
        last_rsi=64.0,
    )
    prepared = _prepared_with_frames(frame_15m, _strategy_frame())

    result = VolumeAnomalySetup(settings=settings).calculate(prepared)

    assert result.signal is not None
    assert result.signal.setup_id == "volume_anomaly"
    assert result.signal.direction == "long"


def test_volume_climax_reversal_detects_long_sweep() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    frame_15m = _strategy_frame(
        last_open=99.8,
        last_close=100.4,
        last_volume_ratio=2.8,
        last_close_position=0.72,
        last_rsi=36.0,
        last_prev_donchian_low=100.0,
        last_prev_donchian_high=103.0,
    )
    frame_15m = frame_15m.with_columns(pl.lit(99.2).alias("low"))
    prepared = _prepared_with_frames(frame_15m, _strategy_frame())

    result = VolumeClimaxReversalSetup(settings=settings).calculate(prepared)

    assert result.signal is not None
    assert result.signal.setup_id == "volume_climax_reversal"
    assert result.signal.direction == "long"


def test_keltner_breakout_detects_long_breakout() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    frame_15m = _strategy_frame(
        last_open=101.2,
        last_close=102.4,
        last_volume_ratio=1.7,
        last_kc_upper=101.8,
        last_kc_lower=100.0,
    )
    prepared = _prepared_with_frames(frame_15m, _strategy_frame())

    result = KeltnerBreakoutSetup(settings=settings).calculate(prepared)

    assert result.signal is not None
    assert result.signal.setup_id == "keltner_breakout"
    assert result.signal.direction == "long"

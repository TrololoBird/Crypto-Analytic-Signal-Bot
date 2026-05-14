from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from bot.domain.config import BotSettings
from bot.domain.schemas import PreparedSymbol, UniverseSymbol
from bot.strategies import (
    BreakerBlockSetup,
    HiddenDivergenceSetup,
    MultiTFTrendSetup,
    SqueezeSetup,
    STRATEGY_CLASSES,
    StructureBreakRetestSetup,
    TurtleSoupSetup,
)
from bot.strategies.bos_choch import (
    _select_external_stop_level,
    _select_stop_level_with_fallback,
)
from bot.strategies.keltner_breakout import KeltnerBreakoutSetup
from bot.strategies.price_velocity import PriceVelocitySetup
from bot.strategies.session_killzone import SessionKillzoneSetup, _active_killzone_name
from bot.setups.smc import SMCZone
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


def _prepared_with_frames(frame_15m: pl.DataFrame, frame_1h: pl.DataFrame) -> PreparedSymbol:
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


def _hidden_divergence_1h_frame(rows: int = 40) -> pl.DataFrame:
    now = datetime.now(UTC)
    close = [105.0 for _ in range(rows)]
    high = [106.0 + (idx % 3) * 0.1 for idx in range(rows)]
    low = [104.0 + (idx % 3) * 0.1 for idx in range(rows)]
    rsi = [50.0 for _ in range(rows)]

    low[8] = 105.0
    low[9] = 104.0
    low[10] = 100.0
    low[11] = 104.0
    low[12] = 105.0
    rsi[10] = 45.0

    high[18] = 107.0
    high[19] = 109.0
    high[20] = 112.0
    high[21] = 109.0
    high[22] = 107.0

    low[28] = 105.0
    low[29] = 104.0
    low[30] = 103.0
    low[31] = 104.0
    low[32] = 105.0
    rsi[30] = 35.0

    close[-1] = 105.0
    high[-1] = 106.0
    low[-1] = 104.5
    rsi[-1] = 45.0
    return pl.DataFrame(
        {
            "open_time": [now for _ in range(rows)],
            "close_time": [now for _ in range(rows)],
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": [1000.0 for _ in range(rows)],
            "atr14": [1.0 for _ in range(rows)],
            "rsi14": rsi,
            "adx14": [25.0 for _ in range(rows)],
            "volume_ratio20": [1.2 for _ in range(rows)],
        }
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


def test_session_killzone_rejects_momentum_without_range_break() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    now = datetime(2026, 5, 12, 8, 45, tzinfo=UTC)
    frame_15m = _strategy_frame(
        last_open=101.6,
        last_close=102.2,
        last_volume_ratio=1.8,
        last_close_position=0.72,
        last_adx=25.0,
    ).with_columns(
        pl.lit(now).alias("time"),
        pl.when(pl.arange(0, pl.len()) == pl.len() - 1)
        .then(pl.lit(102.9))
        .otherwise(pl.lit(104.0))
        .alias("high"),
    )
    prepared = _prepared_with_frames(frame_15m, _strategy_frame(last_adx=25.0))
    prepared.depth_imbalance = 0.25
    prepared.microprice_bias = 0.25

    result = SessionKillzoneSetup(settings=settings).calculate(prepared)

    assert result.signal is None
    assert result.decision.reason_code.endswith("session_breakout_missing")


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
    prepared = _prepared_with_frames(frame_15m, _strategy_frame(last_supertrend_dir=1.0))

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


def test_price_velocity_rejects_short_against_orderbook_support() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    frame_15m = _strategy_frame(
        last_open=102.2,
        last_close=100.6,
        last_volume_ratio=2.2,
        last_close_position=0.20,
        last_rsi=34.0,
        last_roc10=-1.4,
    )
    prepared = _prepared_with_frames(frame_15m, _strategy_frame())
    prepared.depth_imbalance = 0.28
    prepared.microprice_bias = 0.28

    result = PriceVelocitySetup(settings=settings).calculate(prepared)

    assert result.signal is None
    assert result.decision.reason_code.endswith("orderflow_against_velocity")


def test_multi_tf_trend_rejects_overextended_trend_chase() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    frame_15m = _strategy_frame(
        last_open=101.1,
        last_close=102.4,
        last_volume_ratio=2.0,
        last_rsi=77.0,
    )
    prepared = _prepared_with_frames(frame_15m, _strategy_frame(last_adx=28.0))

    result = MultiTFTrendSetup(settings=settings).calculate(prepared)

    assert result.signal is None
    assert result.decision.reason_code.endswith("pullback_quality_missing")


def test_turtle_soup_rejects_long_without_orderflow_recovery() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    now = datetime(2026, 5, 12, 8, 45, tzinfo=UTC)
    rows = 25
    frame_1h = pl.DataFrame(
        {
            "open_time": [now for _ in range(rows)],
            "close_time": [now for _ in range(rows)],
            "open": [101.0 for _ in range(rows)],
            "high": [102.0 for _ in range(rows)],
            "low": [100.0 for _ in range(rows)],
            "close": [101.0 for _ in range(rows)],
            "volume": [1000.0 for _ in range(rows)],
            "atr14": [1.0 for _ in range(rows)],
            "adx14": [25.0 for _ in range(rows)],
            "rsi14": [35.0 for _ in range(rows)],
            "volume_ratio20": [1.5 for _ in range(rows)],
        }
    )
    frame_1h[-1, "low"] = 98.8
    frame_1h[-1, "close"] = 100.6
    frame_15m = _strategy_frame(
        rows=30,
        last_open=100.2,
        last_close=100.8,
        last_volume_ratio=2.0,
        last_rsi=31.0,
    )
    prepared = _prepared_with_frames(frame_15m, frame_1h)
    prepared.depth_imbalance = -0.08
    prepared.microprice_bias = -0.08

    result = TurtleSoupSetup(settings=settings).calculate(prepared)

    assert result.signal is None
    assert result.decision.reason_code.endswith("orderflow_recovery_missing")


def test_breaker_block_rejects_low_volume_retest(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    frame_15m = _strategy_frame(last_volume_ratio=0.55)
    prepared = _prepared_with_frames(frame_15m, _strategy_frame(last_volume_ratio=0.55))
    prepared.mark_price = prepared.universe.last_price

    def fake_breaker_block(*_args: object, **_kwargs: object) -> SMCZone:
        return SMCZone(
            kind="breaker_block",
            direction="long",
            top=103.0,
            bottom=99.0,
            created_index=10,
            state="invalidated",
            midpoint=101.0,
            width=4.0,
            metadata={"source_ob_direction": "short"},
        )

    monkeypatch.setattr(
        "bot.strategies.breaker_block.latest_breaker_block",
        fake_breaker_block,
    )

    result = BreakerBlockSetup(settings=settings).calculate(prepared)

    assert result.signal is None
    assert result.decision.reason_code.endswith("volume_too_low")


def test_hidden_divergence_uses_configured_min_volume_ratio() -> None:
    settings = BotSettings(
        tg_token="1" * 30,
        target_chat_id="123",
        filters={"setups": {"hidden_divergence": {"min_volume_ratio": 0.55}}},
    )
    frame_15m = _strategy_frame(
        rows=40,
        last_open=104.8,
        last_close=105.0,
        last_volume_ratio=0.70,
        last_close_position=0.68,
        last_rsi=45.0,
    )
    prepared = _prepared_with_frames(frame_15m, _hidden_divergence_1h_frame())

    result = HiddenDivergenceSetup(settings=settings).calculate(prepared)

    assert result.signal is not None
    assert result.signal.setup_id == "hidden_divergence"
    assert result.signal.direction == "long"


def test_structure_break_retest_uses_15m_range_retest_fallback() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    frame_15m = _strategy_frame(
        last_open=101.9,
        last_close=102.45,
        prev_close=101.82,
        last_volume_ratio=1.55,
        last_close_position=0.78,
        last_rsi=58.0,
        last_prev_donchian_high=102.0,
        last_prev_donchian_low=99.0,
    ).with_columns(
        pl.when(pl.arange(0, pl.len()) == pl.len() - 1)
        .then(pl.lit(101.72))
        .otherwise(pl.col("low"))
        .alias("low")
    )
    prepared = _prepared_with_frames(frame_15m, _strategy_frame())

    result = StructureBreakRetestSetup(settings=settings).calculate(prepared)

    assert result.signal is not None
    assert result.signal.setup_id == "structure_break_retest"
    assert result.signal.direction == "long"
    assert any("15m" in reason for reason in result.signal.reasons)


def test_squeeze_setup_detects_recent_compression_release_without_crowd_extreme() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    rows = 40
    frame_15m = _strategy_frame(
        rows=rows,
        last_open=101.7,
        last_close=102.8,
        last_volume_ratio=1.45,
        last_close_position=0.84,
        last_rsi=59.0,
        last_kc_upper=102.2,
        last_kc_lower=99.0,
    ).with_columns(
        pl.Series("bb_width", [0.018 for _ in range(rows - 1)] + [0.075]),
        pl.Series("bb_pct_b", [0.55 for _ in range(rows - 1)] + [0.87]),
    )
    prepared = _prepared_with_frames(frame_15m, _strategy_frame())
    prepared.funding_rate = 0.0
    prepared.liquidation_score = None

    result = SqueezeSetup(settings=settings).calculate(prepared)

    assert result.signal is not None
    assert result.signal.setup_id == "squeeze_setup"
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


def test_volume_climax_reversal_accepts_strong_wick_with_adaptive_volume() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    frame_15m = _strategy_frame(
        last_open=100.0,
        last_close=100.6,
        last_volume_ratio=1.35,
        last_close_position=0.76,
        last_rsi=37.0,
        last_prev_donchian_low=100.2,
        last_prev_donchian_high=103.0,
    ).with_columns(
        pl.when(pl.arange(0, pl.len()) == pl.len() - 1)
        .then(pl.lit(99.1))
        .otherwise(pl.col("low"))
        .alias("low")
    )
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

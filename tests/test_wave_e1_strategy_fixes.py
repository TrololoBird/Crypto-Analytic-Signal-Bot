"""Unit tests for Wave E1 strategy/engine fixes (no network)."""

from __future__ import annotations

import polars as pl
import pytest

from bot.domain.config import BotSettings, DeliveryConfig, FilterConfig, RuntimeConfig
from bot.domain.schemas import Signal
from bot.domain.strategies import SignalResult, StrategyDecision
from bot.engine.engine import SignalEngine
from bot.strategies.bos_choch import BOSCHOCHSetup
from bot.strategies.fvg import FVGSetup, detect_fvg
from bot.strategies.liquidity_sweep import LiquiditySweepSetup, detect_liquidity_sweep


def _ohlc_frame(rows: list[tuple[float, float, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "open": [row[0] for row in rows],
            "high": [row[1] for row in rows],
            "low": [row[2] for row in rows],
            "close": [row[3] for row in rows],
            "volume": [1000.0] * len(rows),
        }
    )


def test_detect_fvg_requires_ict_middle_candle_confirmation() -> None:
    pad = (100.0, 101.0, 99.0, 100.5)
    # Outer wicks gap but middle candle is bearish - strict ICT rejects.
    loose_only = _ohlc_frame(
        [
            pad,
            pad,
            (100.0, 101.0, 99.0, 100.5),
            (100.5, 101.0, 99.5, 100.0),  # bearish middle
            (100.0, 100.8, 103.0, 103.5),
        ]
    )
    assert detect_fvg(loose_only, max_age=5) is None

    strict_bull = _ohlc_frame(
        [
            pad,
            pad,
            (100.0, 101.0, 99.0, 100.5),
            (100.5, 103.0, 100.0, 102.5),  # bullish impulse middle
            (102.5, 103.0, 104.0, 103.5),  # high[0]=101 < low[4]=104
        ]
    )
    hit = detect_fvg(strict_bull, max_age=5)
    assert hit is not None
    assert hit.direction == "long"


def test_detect_liquidity_sweep_uses_sweep_atr_mult_param() -> None:
    n = 25
    frame = pl.DataFrame(
        {
            "open": [99.0] * n,
            "high": [99.5] * (n - 1) + [100.21],
            "low": [98.5] * n,
            "close": [99.0] * (n - 1) + [99.96],
            "volume": [1000.0] * n,
            "spec_atr14": [1.0] * n,
            "spec_prev_high20": [100.0] * n,
            "spec_prev_low20": [98.0] * n,
            "volume_ratio20": [1.0] * n,
            "rsi14": [50.0] * n,
        }
    ).with_row_index("_spec_idx")
    # wick = 0.25 ATR - passes 0.2, fails 0.3
    assert detect_liquidity_sweep(frame, sweep_atr_mult=0.2) is not None
    assert detect_liquidity_sweep(frame, sweep_atr_mult=0.3) is None


@pytest.mark.parametrize(
    ("setup_cls", "key", "expected"),
    [
        (LiquiditySweepSetup, "base_score", 0.54),
        (LiquiditySweepSetup, "sweep_atr_mult", 0.20),
        (BOSCHOCHSetup, "base_score", 0.53),
        (FVGSetup, "base_score", 0.60),
        (FVGSetup, "min_fvg_size_atr", 0.30),
    ],
)
def test_defaults_match_strategy_toml(setup_cls, key: str, expected: float) -> None:
    assert setup_cls.DEFAULTS[key] == expected


def _signal(score: float) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        setup_id="test_setup",
        direction="long",
        score=score,
        timeframe="15m",
        entry_low=99.5,
        entry_high=100.5,
        stop=99.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
    )


def _result(score: float) -> SignalResult:
    signal = _signal(score)
    return SignalResult(
        setup_id="test_setup",
        signal=signal,
        decision=StrategyDecision.signal_hit(setup_id="test_setup", signal=signal),
    )


class _EmptyRegistry:
    def get_enabled(self):
        return []


def test_get_best_signal_uses_watch_min_score_floor() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        runtime=RuntimeConfig(),
        filters=FilterConfig(min_score=0.66),
        delivery=DeliveryConfig(watch_min_score=0.55),
    )
    engine = SignalEngine(_EmptyRegistry(), settings)

    below_watch = engine.get_best_signal([_result(0.50)])
    assert below_watch is None

    above_watch = engine.get_best_signal([_result(0.60), _result(0.58)])
    assert above_watch is not None
    assert above_watch.score == 0.60

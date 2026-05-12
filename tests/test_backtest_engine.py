from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from bot.backtest.engine import VectorizedBacktester
from bot.domain.config import BotSettings


def _synthetic_market(rows: int = 220) -> pl.DataFrame:
    base = datetime(2025, 1, 1, tzinfo=UTC)
    ts = [base + timedelta(minutes=15 * i) for i in range(rows)]
    # Build alternating trend segments so EMA cross strategy can trade both ways.
    close: list[float] = []
    price = 100.0
    for i in range(rows):
        if i < 70:
            price += 0.12
        elif i < 140:
            price -= 0.18
        else:
            price += 0.14
        close.append(price)
    return pl.DataFrame(
        {
            "close_time": ts,
            "open": close,
            "high": [c + 0.4 for c in close],
            "low": [c - 0.4 for c in close],
            "close": close,
            "volume": [1000.0 + (i % 10) * 15.0 for i in range(rows)],
            "signal_long": [1 if i in (50, 150) else 0 for i in range(rows)],
            "signal_short": [1 if i in (100,) else 0 for i in range(rows)],
        }
    )


def test_backtester_simulates_real_trades(tmp_path) -> None:
    settings = BotSettings(tg_token="0" * 30, target_chat_id="0")
    settings.data_dir = tmp_path / "bot"
    parquet_dir = settings.data_dir / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)

    symbol = "BTCUSDT"
    timeframe = "15m"
    data = _synthetic_market()
    data.write_parquet(parquet_dir / f"{symbol}_{timeframe}.parquet")

    engine = VectorizedBacktester(settings)
    result = engine.run(
        symbol=symbol,
        start=data["close_time"].min(),
        end=data["close_time"].max(),
        timeframe=timeframe,
        setup_id="ema_cross",
    )

    assert result.trades.height > 0
    assert "entry_ts" in result.trades.columns
    assert "activation_ts" in result.trades.columns
    assert "exit_ts" in result.trades.columns
    assert "status" in result.trades.columns
    assert "position_leverage" in result.trades.columns
    assert result.equity_curve.height > 0
    assert result.trade_count == result.trades.height
    assert 0.0 <= result.activation_rate <= 1.0


def test_backtester_supports_momentum_breakout(tmp_path) -> None:
    settings = BotSettings(tg_token="0" * 30, target_chat_id="0")
    settings.data_dir = tmp_path / "bot"
    parquet_dir = settings.data_dir / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    symbol = "ETHUSDT"
    timeframe = "15m"
    data = _synthetic_market()
    data.write_parquet(parquet_dir / f"{symbol}_{timeframe}.parquet")
    engine = VectorizedBacktester(settings)
    result = engine.run(
        symbol=symbol,
        start=data["close_time"].min(),
        end=data["close_time"].max(),
        timeframe=timeframe,
        setup_id="momentum_breakout",
    )
    assert result.equity_curve.height > 0
    assert "setup_id" in result.trades.columns or result.trades.is_empty()
    assert result.setup_breakdown is None or "momentum_breakout" in result.setup_breakdown


def test_backtester_uses_signal_columns_when_present(tmp_path) -> None:
    settings = BotSettings(tg_token="0" * 30, target_chat_id="0")
    settings.data_dir = tmp_path / "bot"
    parquet_dir = settings.data_dir / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    symbol = "XRPUSDT"
    timeframe = "15m"
    data = _synthetic_market()
    data.write_parquet(parquet_dir / f"{symbol}_{timeframe}.parquet")
    engine = VectorizedBacktester(settings)
    result = engine.run(
        symbol=symbol,
        start=data["close_time"].min(),
        end=data["close_time"].max(),
        timeframe=timeframe,
        setup_id="ema_cross",
        initial_equity=2.0,
    )
    assert result.equity_curve["equity"].item(0) >= 2.0
    assert result.trade_count >= 0
    assert result.tp1_rate >= result.tp2_rate


def test_backtester_rejects_unknown_setup(tmp_path) -> None:
    settings = BotSettings(tg_token="0" * 30, target_chat_id="0")
    settings.data_dir = tmp_path / "bot"
    parquet_dir = settings.data_dir / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    symbol = "SOLUSDT"
    timeframe = "15m"
    data = _synthetic_market()
    data.write_parquet(parquet_dir / f"{symbol}_{timeframe}.parquet")
    engine = VectorizedBacktester(settings)
    with pytest.raises(ValueError):
        engine.run(
            symbol=symbol,
            start=data["close_time"].min(),
            end=data["close_time"].max(),
            timeframe=timeframe,
            setup_id="unknown_setup",
        )


def test_backtester_supports_lifecycle_metrics_for_all_live_setups(tmp_path) -> None:
    settings = BotSettings(tg_token="0" * 30, target_chat_id="0")
    settings.data_dir = tmp_path / "bot"
    parquet_dir = settings.data_dir / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)

    symbol = "ADAUSDT"
    timeframe = "15m"
    data = _synthetic_market(rows=260)
    data.write_parquet(parquet_dir / f"{symbol}_{timeframe}.parquet")

    live_setups = [
        "structure_pullback",
        "structure_break_retest",
        "wick_trap_reversal",
        "squeeze_setup",
        "ema_bounce",
        "fvg_setup",
        "order_block",
        "liquidity_sweep",
        "bos_choch",
        "hidden_divergence",
        "funding_reversal",
        "cvd_divergence",
        "session_killzone",
        "breaker_block",
        "turtle_soup",
        "vwap_trend",
        "supertrend_follow",
        "price_velocity",
        "volume_anomaly",
        "volume_climax_reversal",
        "keltner_breakout",
        "whale_walls",
        "spread_strategy",
        "depth_imbalance",
        "absorption",
        "aggression_shift",
        "liquidation_heatmap",
        "stop_hunt_detection",
        "multi_tf_trend",
        "rsi_divergence_bottom",
        "wyckoff_spring",
        "bb_squeeze",
        "atr_expansion",
        "ls_ratio_extreme",
        "oi_divergence",
        "btc_correlation",
        "altcoin_season_index",
    ]
    signal_rows = []
    for index, setup_id in enumerate(live_setups, start=20):
        ts = data["close_time"].item(index * 2)
        close = float(data["close"].item(index * 2))
        signal_rows.append(
            {
                "created_at": ts,
                "setup_id": setup_id,
                "direction": "long" if index % 2 == 0 else "short",
                "entry_low": close,
                "entry_high": close,
                "stop": close - 1.0 if index % 2 == 0 else close + 1.0,
                "take_profit_1": close + 1.5 if index % 2 == 0 else close - 1.5,
                "take_profit_2": close + 2.0 if index % 2 == 0 else close - 2.0,
            }
        )

    engine = VectorizedBacktester(settings)
    result = engine.run(
        symbol=symbol,
        start=data["close_time"].min(),
        end=data["close_time"].max(),
        timeframe=timeframe,
        setup_id="ema_bounce",
        signals=pl.DataFrame(signal_rows),
    )

    assert result.trade_count == len(live_setups)
    assert result.setup_breakdown is not None
    assert set(result.setup_breakdown) == set(live_setups)
    assert 0.0 <= result.activation_rate <= 1.0

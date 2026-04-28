from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from bot import features_advanced, features_core, features_oscillators
from bot.features_microstructure import add_microstructure_features
from bot.features import (
    _HAS_TALIB,
    _adx,
    _prepare_frame,
    _stochastic,
    _supertrend,
    plta,
)


def _ohlcv(rows: int = 280) -> pl.DataFrame:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    times = [base + timedelta(minutes=15 * i) for i in range(rows)]
    closes = [100.0 + i * 0.12 + ((i % 9) - 4) * 0.04 for i in range(rows)]
    return pl.DataFrame(
        {
            "open_time": times,
            "close_time": times,
            "open": [c - 0.3 for c in closes],
            "high": [c + 0.6 for c in closes],
            "low": [c - 0.6 for c in closes],
            "close": closes,
            "volume": [1000.0 + i * 1.5 for i in range(rows)],
            "quote_volume": [100000.0 + (10 * i) for i in range(rows)],
            "trades": [100 + i for i in range(rows)],
            "taker_buy_base_volume": [500.0 + (i * 0.2) for i in range(rows)],
            "taker_buy_quote_volume": [50000.0 + (i * 12) for i in range(rows)],
        }
    )


def test_prepare_frame_parity_with_decomposed_pipeline() -> None:
    frame = _ohlcv()
    expected = _prepare_frame(frame)
    work = features_core.add_core_features(frame, plta=plta, has_talib=_HAS_TALIB)
    work = features_advanced.add_advanced_indicators(
        work,
        plta=plta,
        has_talib=_HAS_TALIB,
        atr_fn=features_core.atr,
        roc_fn=features_core.roc,
        log_fallback=lambda *_args, **_kwargs: None,
    )
    work = add_microstructure_features(work)
    work = work.with_columns(
        [
            features_core.roc(work, 10).fill_nan(0.0).alias("roc10"),
            features_core.realized_volatility(work, 20)
            .fill_nan(0.0)
            .alias("realized_vol_20"),
            (
                (
                    pl.col("vwap_deviation_pct")
                    - pl.col("vwap_deviation_pct").rolling_mean(window_size=20)
                )
                / pl.col("vwap_deviation_pct").rolling_std(window_size=20)
            )
            .fill_nan(0.0)
            .alias("vwap_deviation_z20"),
        ]
    ).filter(pl.col("ema200").is_not_null() & pl.col("donchian_low20").is_not_null())

    assert expected.columns == work.columns
    assert expected.equals(work, null_equal=True)


def test_indicator_parity_delegated_modules_match_legacy_exports() -> None:
    frame = _ohlcv(80)
    assert _adx(frame, 14).equals(
        features_core.adx(frame, 14, plta=plta, has_talib=_HAS_TALIB),
        null_equal=True,
    )
    legacy_st, legacy_dir = _supertrend(frame, 10, 3.0)
    module_st, module_dir = features_advanced.supertrend(frame, 10, 3.0)
    assert legacy_st.equals(module_st, null_equal=True)
    assert legacy_dir.equals(module_dir, null_equal=True)
    legacy_k, legacy_d = _stochastic(frame)
    module_k, module_d = features_oscillators.stochastic(frame)
    assert legacy_k.equals(module_k, null_equal=True)
    assert legacy_d.equals(module_d, null_equal=True)

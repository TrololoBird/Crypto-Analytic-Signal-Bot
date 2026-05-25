from __future__ import annotations

import math

import numpy as np
import polars as pl

from bot import features
from bot import features_core


def sample_ohlcv(rows: int = 80) -> pl.DataFrame:
    idx = np.arange(rows, dtype=float)
    close = 100.0 + np.sin(idx / 3.0) * 3.0 + idx * 0.21
    open_ = close + np.cos(idx / 5.0) * 0.35
    high = np.maximum(open_, close) + 1.1 + (idx % 4) * 0.05
    low = np.minimum(open_, close) - 1.0 - (idx % 3) * 0.04
    volume = 1000.0 + (idx % 9) * 37.0 + idx * 4.0
    return pl.DataFrame(
        {
            "open_time": (1_700_000_000_000 + idx * 60_000).astype(np.int64),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "quote_volume": volume * close,
            "num_trades": (100 + idx).astype(np.int64),
            "taker_buy_base_volume": volume * 0.52,
            "taker_buy_quote_volume": volume * close * 0.52,
        }
    )


def wilder_reference(values: np.ndarray, period: int, seed_offset: int = 0) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=float)
    seed_end = seed_offset + period
    if len(values) < seed_end:
        return out
    clean = np.nan_to_num(values.astype(float), nan=0.0, posinf=0.0, neginf=0.0)
    seed_idx = seed_end - 1
    out[seed_idx] = clean[seed_offset:seed_end].mean()
    for idx in range(seed_idx + 1, len(clean)):
        out[idx] = (out[idx - 1] * (period - 1) + clean[idx]) / period
    return out


def atr_reference(df: pl.DataFrame, period: int = 14) -> np.ndarray:
    high = np.asarray(df["high"], dtype=float)
    low = np.asarray(df["low"], dtype=float)
    close = np.asarray(df["close"], dtype=float)
    previous_close = np.r_[np.nan, close[:-1]]
    true_range = np.maximum.reduce(
        [
            np.abs(high - low),
            np.abs(high - previous_close),
            np.abs(low - previous_close),
        ]
    )
    true_range[0] = abs(high[0] - low[0])
    return wilder_reference(true_range, period)


def rsi_reference(df: pl.DataFrame, period: int = 14) -> np.ndarray:
    close = np.asarray(df["close"], dtype=float)
    delta = np.r_[np.nan, np.diff(close)]
    gain = np.where(delta > 0.0, delta, 0.0)
    loss = np.where(delta < 0.0, -delta, 0.0)
    gain[0] = np.nan
    loss[0] = np.nan
    avg_gain = wilder_reference(gain, period, seed_offset=1)
    avg_loss = wilder_reference(loss, period, seed_offset=1)
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi[(avg_loss == 0.0) & (avg_gain > 0.0)] = 100.0
    rsi[(avg_gain == 0.0) & (avg_loss > 0.0)] = 0.0
    rsi[(avg_gain == 0.0) & (avg_loss == 0.0)] = 50.0
    return rsi


def assert_series_close(actual: pl.Series, expected: np.ndarray, *, atol: float = 1e-9) -> None:
    actual_np = np.asarray(actual.to_list(), dtype=float)
    mask = ~(np.isnan(actual_np) | np.isnan(expected))
    assert mask.any()
    assert np.max(np.abs(actual_np[mask] - expected[mask])) <= atol


def test_runtime_atr_matches_wilder_reference() -> None:
    df = sample_ohlcv()
    assert_series_close(features._atr(df, 14), atr_reference(df, 14))


def test_runtime_rsi_matches_wilder_reference() -> None:
    df = sample_ohlcv()
    assert_series_close(features._rsi(df, 14), rsi_reference(df, 14))


def test_features_core_atr_matches_wilder_reference_even_when_backend_flag_true() -> None:
    df = sample_ohlcv()
    assert_series_close(
        features_core.atr(df, 14, plta=object(), has_talib=True),
        atr_reference(df, 14),
    )


def test_features_core_rsi_matches_wilder_reference_even_when_backend_flag_true() -> None:
    df = sample_ohlcv()
    assert_series_close(
        features_core.rsi(df, 14, plta=object(), has_talib=True),
        rsi_reference(df, 14),
    )


def test_ewm_span_is_not_wilder_for_atr() -> None:
    df = sample_ohlcv()
    high_low = np.asarray(df["high"] - df["low"], dtype=float)
    span_wrong = pl.Series(high_low).ewm_mean(span=14, adjust=False).to_numpy()
    expected = atr_reference(df, 14)
    mask = ~(np.isnan(span_wrong) | np.isnan(expected))
    assert np.max(np.abs(span_wrong[mask] - expected[mask])) > 0.01


def test_flat_market_rsi_is_neutral_not_nan_or_inf() -> None:
    df = pl.DataFrame(
        {
            "open": [100.0] * 60,
            "high": [100.0] * 60,
            "low": [100.0] * 60,
            "close": [100.0] * 60,
            "volume": [1000.0] * 60,
        }
    )
    rsi = features._rsi(df, 14)
    mature = [value for value in rsi.to_list() if value is not None]
    assert mature
    assert all(math.isfinite(float(value)) for value in mature)
    assert mature[-1] == 50.0


def test_prepare_frame_has_no_nan_or_inf_after_warmup_drop() -> None:
    prepared = features._prepare_frame(sample_ohlcv(260))
    assert prepared.height > 0
    float_columns = [
        column
        for column, dtype in prepared.schema.items()
        if dtype in (pl.Float32, pl.Float64)
    ]
    for column in float_columns:
        series = prepared[column]
        assert not bool(series.is_nan().any()), column
        assert not bool(series.is_infinite().any()), column


def test_empty_wilder_inputs_return_empty_series() -> None:
    empty = pl.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []})
    assert features._atr(empty, 14).to_list() == []
    assert features._rsi(empty, 14).to_list() == []

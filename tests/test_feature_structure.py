"""Sanity checks for bot.features.structure (stale-by-mtime but on hot path)."""

from __future__ import annotations

import math

import polars as pl

from bot.features.structure import hull_moving_average, ichimoku_lines, weighted_moving_average


def _sample_ohlcv(rows: int = 80) -> pl.DataFrame:
    close = [100.0 + (i * 0.15) for i in range(rows)]
    return pl.DataFrame(
        {
            "open": close,
            "high": [value + 0.5 for value in close],
            "low": [value - 0.5 for value in close],
            "close": close,
            "volume": [1_000.0 + i for i in range(rows)],
        }
    )


def test_weighted_moving_average_leading_bars_stay_null() -> None:
    series = weighted_moving_average(pl.Series("close", [1.0, 2.0, 3.0, 4.0, 5.0]), 3, name="wma3")
    assert series.null_count() >= 2
    assert len(series.drop_nulls()) >= 1
    tail = series.drop_nulls().tail(1).item()
    assert tail is not None and math.isfinite(float(tail))


def test_hull_and_ichimoku_no_all_zero_fill() -> None:
    frame = _sample_ohlcv()
    hma = hull_moving_average(frame["close"], period=21, name="hma21")
    assert hma.null_count() > 0 or hma.is_nan().sum() > 0
    finite_hma = hma.filter(hma.is_finite())
    assert len(finite_hma) > 0
    assert not finite_hma.eq(0.0).all()

    tenkan, kijun, senkou_a, senkou_b = ichimoku_lines(frame)
    for label, col in (
        ("tenkan", tenkan),
        ("kijun", kijun),
        ("senkou_a", senkou_a),
        ("senkou_b", senkou_b),
    ):
        assert col.null_count() > 0 or col.is_nan().sum() > 0, label
        finite = col.filter(col.is_finite())
        assert len(finite) > 0, label

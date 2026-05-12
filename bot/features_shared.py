from __future__ import annotations

import math

import polars as pl

REQUIRED_OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


def ensure_columns(
    df: pl.DataFrame, required: tuple[str, ...], *, fn_name: str
) -> None:
    missing = [name for name in required if name not in df.columns]
    if missing:
        raise ValueError(
            f"{fn_name} requires columns={required}, missing={tuple(missing)}"
        )


def clean_non_finite(series: pl.Series, *, fill: float) -> pl.Series:
    """Replace NaN/inf/null values with a stable fill value."""
    return (
        series.replace([float("inf"), float("-inf")], None)
        .fill_nan(fill)
        .fill_null(fill)
    )


def materialize_series(
    value: pl.Series | pl.Expr | int | float,
    *,
    df: pl.DataFrame,
    name: str,
) -> pl.Series:
    if isinstance(value, pl.Series):
        return value.rename(name)
    if isinstance(value, pl.Expr):
        return df.select(value.alias(name)).to_series()
    return pl.Series(name, [value] * df.height, dtype=pl.Float64)


def finite_float(value: object, *, fill: float = 0.0) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fill
    return result if math.isfinite(result) else fill


def wilder_mean(
    series: pl.Series,
    *,
    period: int,
    name: str,
    seed_offset: int = 0,
) -> pl.Series:
    """Wilder running average seeded with a simple mean over the first window."""
    size = len(series)
    period = max(1, int(period))
    seed_end = int(seed_offset) + period
    if size < seed_end:
        return pl.Series(name, [None] * size, dtype=pl.Float64)

    # Vectorized Wilder: use ewm_mean with alpha=1/period, seeded with SMA
    # Replace non-finite values to ensure stability, matching finite_float behavior
    clean_series = (
        series.replace([float("inf"), float("-inf")], None)
        .fill_nan(0.0)
        .fill_null(0.0)
        .cast(pl.Float64)
    )

    # Compute seeding SMA
    sma = clean_series.slice(seed_offset, period).mean()
    if sma is None:
        sma = 0.0

    # Construct input for EWM: seed value followed by subsequent raw values
    subsequent = clean_series.slice(seed_end, size - seed_end)
    ewm_input = pl.concat([pl.Series([sma], dtype=pl.Float64), subsequent])

    # Wilder smoothing is exactly ewm(alpha=1/period, adjust=False)
    ewm_output = ewm_input.ewm_mean(alpha=1.0 / period, adjust=False)

    # Align with original series length by prepending nulls
    result = pl.concat(
        [
            pl.Series([None] * (seed_end - 1), dtype=pl.Float64),
            ewm_output,
        ]
    )

    return result.rename(name)


def true_range(df: pl.DataFrame, *, name: str = "true_range") -> pl.Series:
    ensure_columns(df, REQUIRED_OHLCV_COLUMNS, fn_name="true_range")
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    return materialize_series(
        pl.max_horizontal(
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ),
        df=df,
        name=name,
    )


def atr_from_true_range(
    tr: pl.Series,
    *,
    period: int,
    df: pl.DataFrame,
    name: str,
) -> pl.Series:
    return materialize_series(
        tr.ewm_mean(alpha=1.0 / period, adjust=False),
        df=df,
        name=name,
    )

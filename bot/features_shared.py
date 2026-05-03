from __future__ import annotations


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

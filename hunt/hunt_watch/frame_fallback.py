"""Young-listing frame fallbacks — synthesize 4h from 1h when native 4h prepare fails."""

from __future__ import annotations

from typing import Any

import polars as pl

from engine.features.prepare import _prepare_frame

MIN_1H_BARS_FOR_SYNTH = 48
MIN_SYNTH_4H_BARS = 12
THIN_4H_RAW_BARS = 100


def synth_4h_from_1h(df_1h: Any) -> pl.DataFrame | None:
    """Roll up 1h OHLCV into synthetic 4h bars (4×1h) for indicator warmup."""
    if df_1h is None:
        return None
    if hasattr(df_1h, "is_empty") and df_1h.is_empty():
        return None
    if not isinstance(df_1h, pl.DataFrame):
        df_1h = pl.DataFrame(df_1h)
    if df_1h.height < MIN_1H_BARS_FOR_SYNTH:
        return None
    sort_col = "open_time" if "open_time" in df_1h.columns else None
    work = df_1h.sort(sort_col) if sort_col else df_1h
    agg: list[pl.Expr] = [
        pl.col("open").first(),
        pl.col("high").max(),
        pl.col("low").min(),
        pl.col("close").last(),
    ]
    if "volume" in work.columns:
        agg.append(pl.col("volume").sum())
    if sort_col:
        agg.insert(0, pl.col(sort_col).first())
    out = (
        work.with_row_index("_i")
        .with_columns((pl.col("_i") // 4).alias("_g"))
        .group_by("_g")
        .agg(agg)
        .sort(sort_col if sort_col else "_g")
        .drop("_g", strict=False)
    )
    return out if out.height >= MIN_SYNTH_4H_BARS else None


def _work_empty(work: Any) -> bool:
    if work is None:
        return True
    if hasattr(work, "is_empty"):
        return bool(work.is_empty())
    return False


def patch_work_4h(prepared: Any, kline_map: dict[str, Any]) -> bool:
    """Attach synth or raw 4h work frame when native work_4h is missing."""
    if not _work_empty(getattr(prepared, "work_4h", None)):
        return False
    df_1h = kline_map.get("1h")
    synth = synth_4h_from_1h(df_1h)
    if synth is None:
        return False
    work = _prepare_frame(synth)
    if _work_empty(work):
        work = synth
    setattr(prepared, "work_4h", work)
    return True


def should_use_young_lite_path(*, bars_4h: int, bars_1h: int) -> bool:
    """Skip doomed relaxed prepare when 4h history is thin but 1h is usable."""
    return bars_1h >= MIN_1H_BARS_FOR_SYNTH and bars_4h < THIN_4H_RAW_BARS

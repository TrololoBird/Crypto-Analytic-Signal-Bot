"""Incremental vol-adjusted fused-magnitude history (O(1) per new bar).

Recomputing ``fuse(compute_factors(window))`` for every prior bar on each live tick
was O(n²). This module appends one magnitude per new row and returns the trailing
history for the self-calibrated quantile gate.
"""
from __future__ import annotations

import polars as pl

from hunt_core.scanner.detect import fusion as Fz
from hunt_core.scanner.detect.factors import compute_factors
from hunt_core.scanner.detect.fusion import bar_vol_adjusted_magnitude
from hunt_core.scanner.detect.windows import FeatureWindow, build_window

_cache: dict[tuple[str, str], tuple[int, list[float]]] = {}


def clear_magnitude_cache() -> None:
    _cache.clear()


def bar_magnitude(window: FeatureWindow) -> float:
    """Vol-adjusted fused magnitude for one decision bar."""
    return bar_vol_adjusted_magnitude(Fz.fuse(compute_factors(window)), window)


def magnitude_history_for_frame(
    frame: pl.DataFrame,
    *,
    symbol: str,
    tf: str = "15m",
    lookback: int,
    reset: bool = False,
) -> pl.Series | None:
    """Trailing vol-adjusted magnitude series for bars strictly before the last row."""
    sym = symbol.upper()
    key = (sym, tf)
    if reset or key not in _cache:
        _cache[key] = (0, [])

    prev_h, mags = _cache[key]
    height = frame.height
    if height < prev_h:
        mags = []
        prev_h = 0

    for i in range(prev_h, height):
        wi = build_window(frame.head(i + 1), symbol=sym, tf=tf, lookback=lookback)
        mags.append(bar_magnitude(wi))

    _cache[key] = (height, mags)
    return pl.Series(mags[:-1], dtype=pl.Float64) if len(mags) > 1 else None


__all__ = [
    "bar_magnitude",
    "clear_magnitude_cache",
    "magnitude_history_for_frame",
]

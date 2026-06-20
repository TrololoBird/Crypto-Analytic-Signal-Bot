"""Trailing feature window — the single input surface for the fusion engine.

Both the live tick path and the offline replay harness build a window the same way:
take the frame of feature rows *up to and including the current bar* and tail it to a
bounded lookback. Because we only ever read trailing rows, there is no lookahead by
construction — replay passes ``df[0 : i + 1]`` and gets identical behaviour to live.

The window is **column-presence tolerant**: the persisted parquet lake carries ~33
columns while the live frame carries ~75. A factor asks the window for the columns it
needs; missing columns return ``None`` and that factor abstains. This lets one code
path run against both the thin lake (replay) and the rich live frame.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

# Bounded calibration history. This caps how far back a distribution-relative statistic
# looks; it is a sample-size/window choice (not a detection threshold). Large enough to
# cover CUSUM span (96) plus a robust-z window with headroom.
DEFAULT_LOOKBACK = 240


@dataclass(frozen=True)
class FeatureWindow:
    """Immutable trailing slice of feature rows ending at the current bar."""

    symbol: str
    tf: str
    frame: pl.DataFrame

    @property
    def height(self) -> int:
        return self.frame.height

    def has(self, name: str) -> bool:
        return name in self.frame.columns and self.frame.height > 0

    def col(self, name: str) -> pl.Series | None:
        """Trailing series for ``name`` (finite-cast), or ``None`` if absent/empty."""
        if name not in self.frame.columns or self.frame.height == 0:
            return None
        return self.frame.get_column(name).cast(pl.Float64, strict=False)

    def last(self, name: str) -> float | None:
        """Last finite value of ``name`` at the current bar, or ``None``."""
        if name not in self.frame.columns or self.frame.height == 0:
            return None
        val = self.frame.get_column(name)[-1]
        if val is None:
            return None
        try:
            f = float(val)
        except (TypeError, ValueError):
            return None
        return f if np.isfinite(f) else None

    @property
    def close(self) -> pl.Series | None:
        return self.col("close") if self.has("close") else self.col("price")

    @property
    def price(self) -> float | None:
        return self.last("price") if self.has("price") else self.last("close")


def build_window(
    frame: pl.DataFrame,
    *,
    symbol: str,
    tf: str = "15m",
    lookback: int = DEFAULT_LOOKBACK,
    ts_max: str | None = None,
) -> FeatureWindow:
    """Build a trailing window from frame rows ending at the current bar.

    When ``ts_max`` is set and a ``ts`` column exists, rows strictly after that
    timestamp are excluded before tailing — defense-in-depth against lookahead if
    the caller's frame ordering is wrong.
    """
    if frame is None or frame.height == 0:
        empty = frame if frame is not None else pl.DataFrame()
        return FeatureWindow(symbol=symbol.upper(), tf=tf, frame=empty)
    work = frame
    if ts_max is not None and "ts" in work.columns:
        work = work.filter(pl.col("ts") <= ts_max)
    trimmed = work.tail(lookback) if lookback > 0 and work.height > lookback else work
    return FeatureWindow(symbol=symbol.upper(), tf=tf, frame=trimmed)


__all__ = ["DEFAULT_LOOKBACK", "FeatureWindow", "build_window"]

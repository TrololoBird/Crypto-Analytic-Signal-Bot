"""Self-calibration primitives — distribution-relative statistics.

Market-tuned thresholds (RSI 66, fall 3%) are replaced by each symbol's own trailing
distribution. **Sample-size floors, gate quantiles, and CUSUM design constants remain**
— see ``detect/config.py`` and ``docs/FUSION_PARAMS.md`` for the official registry.

Cold-start: fewer than ``min_n`` samples ⇒ ``None`` (abstain, never a neutral default).
"""
from __future__ import annotations

import numpy as np
import polars as pl

# Sample-size floors are the only tunables in this module: they answer "do we have
# enough history to trust a distribution-relative statistic?", not "what counts as a
# signal". A statistic over fewer points than this abstains.
MIN_N_DEFAULT = 30

# MAD→σ consistency constant for normally distributed data (mathematical, not tunable).
_MAD_TO_SIGMA = 1.4826


def _robust_scale(arr: np.ndarray, *, mad_epsilon: float) -> float:
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    scale = max(_MAD_TO_SIGMA * mad, mad_epsilon)
    if scale <= mad_epsilon:
        std = float(np.std(arr))
        if std <= mad_epsilon:
            return mad_epsilon
        return max(std, mad_epsilon)
    return scale


def _clip_z(z: float, *, clip: float) -> float:
    if not np.isfinite(z):
        return 0.0
    return float(max(-clip, min(clip, z)))


def _clean(series: pl.Series | None) -> np.ndarray:
    """Float64 numpy view with nulls/NaNs/inf removed (order preserved)."""
    if series is None or series.len() == 0:
        return np.empty(0, dtype=np.float64)
    arr = series.cast(pl.Float64, strict=False).to_numpy()
    return arr[np.isfinite(arr)]


def robust_z(
    series: pl.Series | None,
    *,
    min_n: int = MIN_N_DEFAULT,
    mad_epsilon: float | None = None,
    clip: float | None = None,
) -> float | None:
    """Robust z of the last value vs its trailing window (median / MAD)."""
    from hunt_core.scanner.detect.config import fusion_params
    from hunt_core.analysis.robust_stats import robust_z as _shared_robust_z

    fp = fusion_params()
    eps = mad_epsilon if mad_epsilon is not None else fp.mad_epsilon
    z_clip = clip if clip is not None else fp.robust_z_clip
    return _shared_robust_z(series, min_n=min_n, mad_epsilon=eps, clip=z_clip)


def robust_z_of(
    value: float | None,
    series: pl.Series | None,
    *,
    min_n: int = MIN_N_DEFAULT,
) -> float | None:
    """Robust z of an explicit ``value`` against the distribution in ``series``."""
    if value is None or not np.isfinite(value):
        return None
    arr = _clean(series)
    if arr.size < min_n:
        return None
    from hunt_core.scanner.detect.config import fusion_params

    fp = fusion_params()
    scale = _robust_scale(arr, mad_epsilon=fp.mad_epsilon)
    median = float(np.median(arr))
    if scale <= fp.mad_epsilon and float(np.std(arr)) <= fp.mad_epsilon:
        return 0.0
    return _clip_z((float(value) - median) / scale, clip=fp.robust_z_clip)


def pctile_rank(series: pl.Series | None, *, min_n: int = MIN_N_DEFAULT) -> float | None:
    """Fraction of the trailing window ≤ the last value, in ``[0, 1]``.

    A distribution-free measure of "how extreme is the current reading relative to
    this symbol's own recent history" — replaces fixed percentile thresholds.
    """
    arr = _clean(series)
    if arr.size < min_n:
        return None
    last = float(arr[-1])
    return float(np.mean(arr <= last))


def quantile_gate(
    series: pl.Series | None,
    q: float,
    *,
    min_n: int = MIN_N_DEFAULT,
) -> float | None:
    """The ``q``-quantile threshold of the window — a self-calibrated gate level."""
    from hunt_core.analysis.robust_stats import quantile as _shared_quantile

    return _shared_quantile(series, q, min_n=min_n)


def ols_slope(
    series: pl.Series | None,
    *,
    min_n: int = MIN_N_DEFAULT,
    normalize: bool = True,
) -> float | None:
    """Per-bar OLS slope of the window vs bar index."""
    from hunt_core.analysis.robust_stats import ols_slope as _shared_ols_slope

    return _shared_ols_slope(series, min_n=min_n, normalize=normalize)


# --- CUSUM change-point (re-homed from scan/pump_cycle.py) -------------------
# Used by detect/phase.py to derive the PRE/MID activation band per symbol from the
# standardized-return change-point, replacing the 10-state lifecycle FSM.

_CUSUM_CLIP = 500.0


def log_returns(close: pl.Series | None) -> pl.Series:
    """Log returns, null/NaN-safe."""
    if close is None or close.len() == 0:
        return pl.Series([], dtype=pl.Float64)
    c = close.cast(pl.Float64, strict=False)
    return (c / c.shift(1)).log().fill_null(0.0).fill_nan(0.0)


def standardized_returns(returns: pl.Series, *, span: int = 96) -> pl.Series:
    """EWM-standardized returns (mean-reverting anchor) — distribution-relative."""
    if returns.len() == 0:
        return returns
    mu = returns.ewm_mean(span=span, min_periods=8)
    dev = (returns - mu).ewm_std(span=span, min_periods=8).fill_null(1e-6).clip(lower_bound=1e-6)
    return ((returns - mu) / dev).fill_null(0.0).fill_nan(0.0)


def cusum_series(z: pl.Series, *, threshold: float, clip: bool = True) -> pl.Series:
    """Signed CUSUM on standardized values, mean-reverting via EWM anchor."""
    pos = 0.0
    neg = 0.0
    out: list[float] = []
    for v in z.to_list():
        try:
            x = float(v or 0.0)
        except (TypeError, ValueError):
            x = 0.0
        pos = max(0.0, pos + x - threshold * 0.25)
        neg = min(0.0, neg + x + threshold * 0.25)
        raw = pos if x >= 0 else neg
        if clip:
            raw = max(-_CUSUM_CLIP, min(_CUSUM_CLIP, raw))
        out.append(raw)
    return pl.Series(out, dtype=pl.Float64)


def cusum_value(close: pl.Series | None, *, span: int = 96, threshold: float) -> float:
    """Current signed CUSUM of standardized returns (0.0 on insufficient data).

    ``threshold`` here is a *self-calibrated activation band* supplied by the caller
    (derived from the series' own standardized-return scale), not a fixed magic number.
    """
    if close is None or close.len() < 12:
        return 0.0
    z = standardized_returns(log_returns(close), span=span)
    cusum = cusum_series(z, threshold=threshold)
    return float(cusum[-1]) if cusum.len() else 0.0


def symbol_state_tier(n: int, *, cold: int = 30, warm: int = 120) -> str:
    """COLD/WARM/HOT from trailing sample count for per-factor confidence."""
    if n < cold:
        return "COLD"
    if n < warm:
        return "WARM"
    return "HOT"


__all__ = [
    "MIN_N_DEFAULT",
    "cusum_series",
    "cusum_value",
    "log_returns",
    "ols_slope",
    "pctile_rank",
    "quantile_gate",
    "robust_z",
    "robust_z_of",
    "standardized_returns",
    "symbol_state_tier",
]

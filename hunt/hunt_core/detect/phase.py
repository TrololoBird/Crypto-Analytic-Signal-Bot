"""PRE-vs-MID phase with CUSUM change-point + sticky MID hysteresis."""
from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from hunt_core.detect import calibrate as C
from hunt_core.detect.config import fusion_params
from hunt_core.detect.windows import FeatureWindow

PRE_PUMP = "pre_pump"
PRE_DUMP = "pre_dump"
MID = "mid"
NEUTRAL = "neutral"

DEFAULT_Q_PHASE = 0.90


@dataclass
class _PhaseSticky:
    mid_latched: bool = False
    below_band_streak: int = 0


_phase_sticky: dict[str, _PhaseSticky] = {}


def clear_phase_sticky() -> None:
    _phase_sticky.clear()


@dataclass(frozen=True)
class PhaseInfo:
    phase: str
    cusum: float
    band: float | None
    mid: bool
    watch_ok: bool


def assess_phase(
    window: FeatureWindow,
    *,
    side: str,
    q_phase: float | None = None,
    min_n: int | None = None,
) -> PhaseInfo:
    """PRE/MID from CUSUM band; MID latched until |CUSUM| stays below band×ratio."""
    fp = fusion_params()
    q_phase = fp.q_phase if q_phase is None else q_phase
    min_n = fp.min_n if min_n is None else min_n

    sym = window.symbol.upper()
    sticky = _phase_sticky.setdefault(sym, _PhaseSticky())

    close = window.close
    if close is None or close.len() < max(12, min_n):
        return PhaseInfo(NEUTRAL, 0.0, None, False, False)

    cusum_threshold = fp.cusum_k * 4.0
    z = C.standardized_returns(C.log_returns(close), span=fp.cusum_span)
    cusum_series = C.cusum_series(z, threshold=cusum_threshold)
    if cusum_series.len() == 0:
        return PhaseInfo(NEUTRAL, 0.0, None, False, False)

    cusum_now = float(cusum_series[-1])
    band = C.quantile_gate(cusum_series.abs(), q_phase, min_n=min_n)
    raw_mid = band is not None and band > 0.0 and abs(cusum_now) >= band

    if sticky.mid_latched:
        exit_level = band * fp.phase_mid_exit_ratio if band is not None else None
        if exit_level is not None and abs(cusum_now) < exit_level:
            sticky.below_band_streak += 1
        else:
            sticky.below_band_streak = 0
        if sticky.below_band_streak >= fp.phase_mid_exit_bars:
            sticky.mid_latched = False
            sticky.below_band_streak = 0
        else:
            return PhaseInfo(MID, cusum_now, band, True, False)

    if raw_mid:
        sticky.mid_latched = True
        sticky.below_band_streak = 0
        return PhaseInfo(MID, cusum_now, band, True, False)

    if side == "long":
        return PhaseInfo(PRE_PUMP, cusum_now, band, False, True)
    if side == "short":
        return PhaseInfo(PRE_DUMP, cusum_now, band, False, True)
    return PhaseInfo(NEUTRAL, cusum_now, band, False, False)


__all__ = [
    "DEFAULT_Q_PHASE",
    "MID",
    "NEUTRAL",
    "PRE_DUMP",
    "PRE_PUMP",
    "PhaseInfo",
    "assess_phase",
    "clear_phase_sticky",
]

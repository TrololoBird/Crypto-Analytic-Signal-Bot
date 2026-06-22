"""Fuse factor scores into one directional pre-move magnitude + self-calibrated gate.

Directional factors are combined by **signed median** (robust to collinearity and single
factor spikes — avoids Stouffer inflation when book+flow correlate). Amplifier factors
saturate through ``tanh``. The side is cross-checked by a rank vote — internal
disagreement closes the gate.

The gate threshold is the symbol's own recent **vol-adjusted** fused-magnitude quantile
(ATR-normalized so flat/compressed regimes do not auto-trigger). ``fusion_score`` (0–100)
is a directional strength index — **not** a calibrated probability.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

import polars as pl

from hunt_core.scanner.detect import calibrate as C
from hunt_core.scanner.detect.config import fusion_params
from hunt_core.scanner.detect.factors import AMPLIFIER, DIRECTIONAL, FactorScore
from hunt_core.scanner.detect.windows import FeatureWindow

# Defaults re-exported for tests; production reads fusion_params().
MIN_ACTIVE_DIRECTIONAL = 2
ABS_MAGNITUDE_FLOOR = 0.5
VOL_FLOOR_PCT = 0.15
FUSION_SCORE_SCALE = 25.0
DEFAULT_Q_GATE = 0.90


def _fp():
    return fusion_params()


def vol_adjusted_magnitude(magnitude: float, atr_pct: float | None) -> float:
    fp = _fp()
    floor = fp.vol_floor_pct
    try:
        atr = float(atr_pct) if atr_pct is not None else floor
    except (TypeError, ValueError):
        atr = floor
    if not math.isfinite(atr) or atr <= 0:
        atr = floor
    return magnitude / max(floor, atr)


def magnitude_to_fusion_score(magnitude: float) -> float:
    if not math.isfinite(magnitude):
        return 0.0
    return min(100.0, max(0.0, magnitude * _fp().fusion_score_scale))


@dataclass(frozen=True)
class FusionScore:
    """Fused directional reading at the current bar (pre-gate)."""

    side: str  # "long" | "short" | "none"
    z_dir: float  # signed directional aggregate (median of factor z)
    magnitude: float  # |z_dir| * (1 + amp), unsigned raw fused strength
    amp: float  # saturated amplifier blend in [0, 1)
    fusion_score: float  # 0–100 strength index (NOT calibrated P(win))
    n_active: int
    agreement: bool
    parts: dict[str, float] = field(default_factory=dict)

    @property
    def confidence(self) -> float:
        """Deprecated alias — use ``fusion_score / 100`` only for legacy readers."""
        return self.fusion_score / 100.0


@dataclass(frozen=True)
class GateDecision:
    gate_open: bool
    threshold: float | None
    q: float
    reason: str
    vol_adjusted_magnitude: float = 0.0


def fuse(factors: list[FactorScore]) -> FusionScore:
    """Combine factor scores into a directional magnitude (pure, no gate)."""
    directional = [f for f in factors if f.kind == DIRECTIONAL and f.active]
    amplifiers = [f for f in factors if f.kind == AMPLIFIER and f.active]

    n = len(directional)
    if n == 0:
        return FusionScore("none", 0.0, 0.0, 0.0, 0.0, 0, False)

    scores = [f.score for f in directional]
    z_dir = float(statistics.median(scores))
    n_pos = sum(1 for f in directional if f.score > 0)
    n_neg = sum(1 for f in directional if f.score < 0)
    rank_side = (n_pos > n_neg) - (n_pos < n_neg)
    z_sign = (z_dir > 0) - (z_dir < 0)
    agreement = rank_side == 0 or z_sign == 0 or z_sign == rank_side

    amp = 0.0
    if amplifiers:
        amp = sum(math.tanh(max(0.0, f.score)) for f in amplifiers) / len(amplifiers)

    magnitude = abs(z_dir) * (1.0 + amp)
    side = "long" if z_sign > 0 else "short" if z_sign < 0 else "none"
    fusion_score = magnitude_to_fusion_score(magnitude)
    parts = {f.name: f.score for f in directional} | {f.name: f.score for f in amplifiers}
    return FusionScore(side, z_dir, magnitude, amp, fusion_score, n, agreement, parts)


def bar_vol_adjusted_magnitude(fusion: FusionScore, window: FeatureWindow) -> float:
    return vol_adjusted_magnitude(fusion.magnitude, window.last("atr_pct"))


def gate(
    fusion: FusionScore,
    magnitude_history: pl.Series | None,
    *,
    q: float | None = None,
    min_n: int | None = None,
    atr_pct: float | None = None,
) -> GateDecision:
    """Self-calibrated gate: max(symbol quantile, global_gate_floor)."""
    fp = _fp()
    q = fp.q_gate if q is None else q
    min_n = fp.min_n if min_n is None else min_n
    min_active = fp.min_active_factors
    abs_floor = fp.abs_magnitude_floor
    global_floor = fp.global_gate_floor

    if fusion.n_active < min_active:
        return GateDecision(False, None, q, f"insufficient_factors:{fusion.n_active}")
    if not fusion.agreement:
        return GateDecision(False, None, q, "factor_disagreement")

    adj_mag = vol_adjusted_magnitude(fusion.magnitude, atr_pct)
    if adj_mag < abs_floor:
        return GateDecision(False, None, q, "below_abs_floor", vol_adjusted_magnitude=adj_mag)

    sym_threshold = C.quantile_gate(magnitude_history, q, min_n=min_n)
    if sym_threshold is None:
        return GateDecision(False, None, q, "cold_start_no_history", vol_adjusted_magnitude=adj_mag)

    effective = max(sym_threshold, global_floor)
    if adj_mag < effective:
        return GateDecision(
            False, effective, q, "below_calibrated_gate", vol_adjusted_magnitude=adj_mag
        )
    return GateDecision(True, effective, q, "gate_open", vol_adjusted_magnitude=adj_mag)


__all__ = [
    "ABS_MAGNITUDE_FLOOR",
    "DEFAULT_Q_GATE",
    "FUSION_SCORE_SCALE",
    "FusionScore",
    "GateDecision",
    "MIN_ACTIVE_DIRECTIONAL",
    "VOL_FLOOR_PCT",
    "bar_vol_adjusted_magnitude",
    "fuse",
    "gate",
    "magnitude_to_fusion_score",
    "vol_adjusted_magnitude",
]

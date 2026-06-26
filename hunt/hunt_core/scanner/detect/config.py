"""Official fusion-engine hyperparameters (explicit, not hidden).

Self-calibration removes *market-tuned* thresholds (RSI 66, fall 3%, etc.) but cannot
eliminate sample-size floors, gate quantiles, or CUSUM design constants. Those live here
and in ``config.defaults.toml [fusion]`` — the single registry operators tune.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from functools import lru_cache
from typing import Any

from hunt_core.domain.config import load_config_defaults_toml
from hunt_core.params.store import universal_section


@dataclass(frozen=True)
class FusionParams:
    """Explicit fusion tunables — documented in docs/FUSION_PARAMS.md."""

    min_n: int = 18
    lookback: int = 120
    q_gate: float = 0.60
    q_phase: float = 0.85
    min_active_factors: int = 2
    # Gate: effective threshold = max(symbol quantile, global_gate_floor).
    global_gate_floor: float = 0.12
    abs_magnitude_floor: float = 0.08
    vol_floor_pct: float = 0.15
    fusion_score_scale: float = 25.0
    # CUSUM (standardized returns): k slack in σ; span for EWM anchor.
    cusum_k: float = 0.5
    cusum_span: int = 48
    # PRE/MID hysteresis — avoid MID→PRE→MID flicker inside one leg.
    phase_mid_exit_ratio: float = 0.65
    phase_mid_exit_bars: int = 2
    # Funding is step-wise; needs longer history than generic min_n.
    funding_min_n: int = 48
    # Pre-phase gate constants (per-symbol quantile calibration TBD).
    pre_gate_min_energy: int = 1
    pre_gate_min_structure: float = 0.10
    pre_gate_min_magnitude: float = 0.08
    # MAD scale floor + robust-z clip (winsorization).
    mad_epsilon: float = 1e-6
    robust_z_clip: float = 12.0
    # Replay harness (offline only).
    replay_warmup: int = 60
    replay_horizon_bars: int = 16
    replay_target_atr: float = 1.5


def _merge_fusion(raw: dict[str, Any]) -> FusionParams:
    base = FusionParams()
    kw = {f.name: getattr(base, f.name) for f in fields(FusionParams)}
    for f in fields(FusionParams):
        if f.name in raw and raw[f.name] is not None:
            kw[f.name] = raw[f.name]
    return FusionParams(**kw)


@lru_cache(maxsize=1)
def fusion_params() -> FusionParams:
    """Merged [fusion] from config.defaults.toml + hunt_calibration universal block."""
    toml_block = load_config_defaults_toml().get("fusion") or {}
    cal_block = universal_section("fusion")
    return _merge_fusion({**toml_block, **cal_block})


def clear_fusion_params_cache() -> None:
    fusion_params.cache_clear()


__all__ = ["FusionParams", "clear_fusion_params_cache", "fusion_params"]

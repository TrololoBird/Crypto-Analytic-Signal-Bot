"""Backward-compatible re-exports — prefer bot.setups.detectors.<module>."""
from __future__ import annotations

from ._common import (
    SpecHit,
    as_float,
    build_spec_signal,
    finite_or_none,
    required_columns,
    with_spec_columns,
    _latest_values,
    _pivot_rows,
)
from .fvg import detect_fvg
from .bos_choch import detect_bos_choch
from .structure_break_retest import detect_structure_break_retest
from .structure_pullback import detect_structure_pullback
from .ob import detect_order_block
from .breaker_block import detect_breaker_block
from .liquidity_sweep import detect_liquidity_sweep
from .turtle_soup import detect_turtle_soup
from .stop_hunt import detect_stop_hunt
from .wyckoff_spring import detect_wyckoff_spring
from .wick_trap import detect_wick_trap
from .volume_anomaly import detect_volume_anomaly
from .volume_climax import detect_volume_climax_reversal
from .ema_bounce import detect_ema_bounce
from .keltner_breakout import detect_keltner_breakout
from .atr_expansion import detect_atr_expansion
from .bb_squeeze import detect_bb_squeeze_release
from .price_velocity import detect_price_velocity
from .vwap import detect_vwap_reclaim
from .aggression_shift import detect_aggression_shift
from .absorption import detect_absorption
from .indicator_divergence import detect_regular_divergence
from .hidden_divergence import detect_hidden_divergence
from .cvd_divergence import detect_cvd_divergence
from ._common import current_utc_hour

__all__ = [
    "SpecHit",
    "as_float",
    "build_spec_signal",
    "finite_or_none",
    "required_columns",
    "with_spec_columns",
    "detect_fvg",
    "detect_bos_choch",
    "detect_structure_break_retest",
    "detect_structure_pullback",
    "detect_order_block",
    "detect_breaker_block",
    "detect_liquidity_sweep",
    "detect_turtle_soup",
    "detect_stop_hunt",
    "detect_wyckoff_spring",
    "detect_wick_trap",
    "detect_volume_anomaly",
    "detect_volume_climax_reversal",
    "detect_ema_bounce",
    "detect_keltner_breakout",
    "detect_atr_expansion",
    "detect_bb_squeeze_release",
    "detect_price_velocity",
    "detect_vwap_reclaim",
    "detect_aggression_shift",
    "detect_absorption",
    "detect_regular_divergence",
    "detect_hidden_divergence",
    "detect_cvd_divergence",
    "current_utc_hour",
]

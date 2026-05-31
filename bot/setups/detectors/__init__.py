"""Per-strategy detectors (38 setup_ids) — import from here or submodules."""

from ._common import (
    SpecHit,
    as_float,
    build_spec_signal,
    current_utc_hour,
    finite_or_none,
    required_columns,
    with_spec_columns,
)
from .absorption import detect_absorption, detect_absorption_prepared
from .aggression_shift import detect_aggression_shift, detect_aggression_shift_prepared
from .altcoin_season_index import detect_altcoin_season_index
from .atr_expansion import detect_atr_expansion_prepared
from .bb_squeeze import detect_bb_squeeze_release, detect_bb_squeeze_prepared
from .bos_choch import detect_bos_choch
from .breaker_block import detect_breaker_block
from .btc_correlation import detect_btc_correlation
from .cvd_divergence import detect_cvd_divergence
from .depth_imbalance import detect_depth_imbalance
from .ema_bounce import detect_ema_bounce
from .funding_reversal import detect_funding_reversal
from .fvg import detect_fvg
from .hidden_divergence import detect_hidden_divergence
from .indicator_divergence import detect_regular_divergence
from .keltner_breakout import detect_keltner_breakout
from .liquidation_heatmap import detect_liquidation_heatmap
from .liquidity_sweep import detect_liquidity_sweep
from .ls_ratio_extreme import detect_ls_ratio_extreme
from .multi_tf_trend import detect_multi_tf_trend
from .ob import detect_order_block
from .oi_divergence import detect_oi_divergence
from .price_velocity import detect_price_velocity
from .rsi_divergence_bottom import detect_rsi_divergence_bottom
from .session_killzone import detect_session_killzone
from .spread_strategy import detect_spread_strategy
from .stop_hunt import detect_stop_hunt
from .stop_hunt_detection import detect_stop_hunt_detection
from .structure_break_retest import detect_structure_break_retest
from .structure_pullback import detect_structure_pullback
from .supertrend_follow import detect_supertrend_follow
from .turtle_soup import detect_turtle_soup
from .volume_anomaly import detect_volume_anomaly
from .volume_climax import detect_volume_climax_reversal
from .vwap import detect_vwap_reclaim
from .vwap_trend import detect_vwap_trend
from .whale_walls import detect_whale_walls
from .wick_trap import detect_wick_trap
from .wyckoff_spring import detect_wyckoff_spring, detect_wyckoff_spring_prepared

__all__ = [
    "SpecHit",
    "as_float",
    "build_spec_signal",
    "current_utc_hour",
    "finite_or_none",
    "required_columns",
    "with_spec_columns",
    "detect_absorption",
    "detect_absorption_prepared",
    "detect_aggression_shift",
    "detect_aggression_shift_prepared",
    "detect_altcoin_season_index",
    "detect_atr_expansion_prepared",
    "detect_bb_squeeze_release",
    "detect_bb_squeeze_prepared",
    "detect_bos_choch",
    "detect_breaker_block",
    "detect_btc_correlation",
    "detect_cvd_divergence",
    "detect_depth_imbalance",
    "detect_ema_bounce",
    "detect_funding_reversal",
    "detect_fvg",
    "detect_hidden_divergence",
    "detect_regular_divergence",
    "detect_keltner_breakout",
    "detect_liquidation_heatmap",
    "detect_liquidity_sweep",
    "detect_ls_ratio_extreme",
    "detect_multi_tf_trend",
    "detect_order_block",
    "detect_oi_divergence",
    "detect_price_velocity",
    "detect_rsi_divergence_bottom",
    "detect_session_killzone",
    "detect_spread_strategy",
    "detect_stop_hunt",
    "detect_stop_hunt_detection",
    "detect_structure_break_retest",
    "detect_structure_pullback",
    "detect_supertrend_follow",
    "detect_turtle_soup",
    "detect_volume_anomaly",
    "detect_volume_climax_reversal",
    "detect_vwap_reclaim",
    "detect_vwap_trend",
    "detect_whale_walls",
    "detect_wick_trap",
    "detect_wyckoff_spring",
    "detect_wyckoff_spring_prepared",
]

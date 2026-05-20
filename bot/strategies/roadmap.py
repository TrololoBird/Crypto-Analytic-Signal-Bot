"""Compatibility exports for roadmap-era strategy modules."""

from __future__ import annotations

from .absorption import AbsorptionSetup
from .aggression_shift import AggressionShiftSetup
from .altcoin_season_index import AltcoinSeasonIndexSetup
from .atr_expansion import ATRExpansionSetup
from .bb_squeeze import BBSqueezeSetup
from .btc_correlation import BTCCorrelationSetup
from .depth_imbalance import DepthImbalanceSetup
from .liquidation_heatmap import LiquidationHeatmapSetup
from .ls_ratio_extreme import LSRatioExtremeSetup
from .multi_tf_trend import MultiTFTrendSetup
from .oi_divergence import OIDivergenceSetup
from .rsi_divergence_bottom import RSIDivergenceBottomSetup
from .spread_strategy import SpreadStrategySetup
from .stop_hunt_detection import StopHuntDetectionSetup
from .whale_walls import WhaleWallsSetup
from .wyckoff_spring import WyckoffSpringSetup

__all__ = [
    "ATRExpansionSetup",
    "AbsorptionSetup",
    "AggressionShiftSetup",
    "AltcoinSeasonIndexSetup",
    "BBSqueezeSetup",
    "BTCCorrelationSetup",
    "DepthImbalanceSetup",
    "LSRatioExtremeSetup",
    "LiquidationHeatmapSetup",
    "MultiTFTrendSetup",
    "OIDivergenceSetup",
    "RSIDivergenceBottomSetup",
    "SpreadStrategySetup",
    "StopHuntDetectionSetup",
    "WhaleWallsSetup",
    "WyckoffSpringSetup",
]

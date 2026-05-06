"""Modern strategy exports."""

from .bos_choch import BOSCHOCHSetup
from .breaker_block import BreakerBlockSetup
from .cvd_divergence import CVDDivergenceSetup
from .ema_bounce import EmaBounceSetup
from .funding_reversal import FundingReversalSetup
from .fvg import FVGSetup
from .hidden_divergence import HiddenDivergenceSetup
from .keltner_breakout import KeltnerBreakoutSetup
from .liquidity_sweep import LiquiditySweepSetup
from .order_block import OrderBlockSetup
from .price_velocity import PriceVelocitySetup
from .roadmap import (
    AbsorptionSetup,
    AggressionShiftSetup,
    AltcoinSeasonIndexSetup,
    ATRExpansionSetup,
    BBSqueezeSetup,
    BTCCorrelationSetup,
    DepthImbalanceSetup,
    LiquidationHeatmapSetup,
    LSRatioExtremeSetup,
    MultiTFTrendSetup,
    OIDivergenceSetup,
    RSIDivergenceBottomSetup,
    SpreadStrategySetup,
    StopHuntDetectionSetup,
    WhaleWallsSetup,
    WyckoffSpringSetup,
)
from .session_killzone import SessionKillzoneSetup
from .squeeze_setup import SqueezeSetup
from .structure_break_retest import StructureBreakRetestSetup
from .structure_pullback import StructurePullbackSetup
from .supertrend_follow import SuperTrendFollowSetup
from .turtle_soup import TurtleSoupSetup
from .volume_anomaly import VolumeAnomalySetup
from .volume_climax_reversal import VolumeClimaxReversalSetup
from .vwap_trend import VWAPTrendSetup
from .wick_trap_reversal import WickTrapReversalSetup
from ..strategy_asset_fit import ASSET_FIT_PROFILES

STRATEGY_CLASSES = (
    StructurePullbackSetup,
    StructureBreakRetestSetup,
    WickTrapReversalSetup,
    SqueezeSetup,
    EmaBounceSetup,
    FVGSetup,
    OrderBlockSetup,
    LiquiditySweepSetup,
    BOSCHOCHSetup,
    HiddenDivergenceSetup,
    FundingReversalSetup,
    CVDDivergenceSetup,
    SessionKillzoneSetup,
    BreakerBlockSetup,
    TurtleSoupSetup,
    VWAPTrendSetup,
    SuperTrendFollowSetup,
    PriceVelocitySetup,
    VolumeAnomalySetup,
    VolumeClimaxReversalSetup,
    KeltnerBreakoutSetup,
    WhaleWallsSetup,
    SpreadStrategySetup,
    DepthImbalanceSetup,
    AbsorptionSetup,
    AggressionShiftSetup,
    LiquidationHeatmapSetup,
    StopHuntDetectionSetup,
    MultiTFTrendSetup,
    RSIDivergenceBottomSetup,
    WyckoffSpringSetup,
    BBSqueezeSetup,
    ATRExpansionSetup,
    LSRatioExtremeSetup,
    OIDivergenceSetup,
    BTCCorrelationSetup,
    AltcoinSeasonIndexSetup,
)

for _strategy_class in STRATEGY_CLASSES:
    _strategy_class.asset_fit = ASSET_FIT_PROFILES[_strategy_class.setup_id]

__all__ = [
    "AbsorptionSetup",
    "AggressionShiftSetup",
    "AltcoinSeasonIndexSetup",
    "ATRExpansionSetup",
    "BBSqueezeSetup",
    "BOSCHOCHSetup",
    "BreakerBlockSetup",
    "BTCCorrelationSetup",
    "CVDDivergenceSetup",
    "DepthImbalanceSetup",
    "EmaBounceSetup",
    "FundingReversalSetup",
    "FVGSetup",
    "HiddenDivergenceSetup",
    "KeltnerBreakoutSetup",
    "LiquiditySweepSetup",
    "LiquidationHeatmapSetup",
    "LSRatioExtremeSetup",
    "MultiTFTrendSetup",
    "OIDivergenceSetup",
    "OrderBlockSetup",
    "PriceVelocitySetup",
    "RSIDivergenceBottomSetup",
    "SessionKillzoneSetup",
    "SqueezeSetup",
    "SpreadStrategySetup",
    "STRATEGY_CLASSES",
    "StopHuntDetectionSetup",
    "StructureBreakRetestSetup",
    "StructurePullbackSetup",
    "SuperTrendFollowSetup",
    "TurtleSoupSetup",
    "VolumeAnomalySetup",
    "VolumeClimaxReversalSetup",
    "VWAPTrendSetup",
    "WhaleWallsSetup",
    "WickTrapReversalSetup",
    "WyckoffSpringSetup",
]

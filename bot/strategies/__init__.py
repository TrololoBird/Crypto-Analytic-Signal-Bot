"""Modern strategy exports."""

from .absorption import AbsorptionSetup
from .aggression_shift import AggressionShiftSetup
from .altcoin_season_index import AltcoinSeasonIndexSetup
from .atr_expansion import ATRExpansionSetup
from .bb_squeeze import BBSqueezeSetup
from .bos_choch import BOSCHOCHSetup
from .breaker_block import BreakerBlockSetup
from .btc_correlation import BTCCorrelationSetup
from .cvd_divergence import CVDDivergenceSetup
from .depth_imbalance import DepthImbalanceSetup
from .ema_bounce import EmaBounceSetup
from .funding_reversal import FundingReversalSetup
from .fvg import FVGSetup
from .hidden_divergence import HiddenDivergenceSetup
from .indicator_divergence import IndicatorDivergenceSetup
from .keltner_breakout import KeltnerBreakoutSetup
from .liquidation_heatmap import LiquidationHeatmapSetup
from .liquidity_sweep import LiquiditySweepSetup
from .ls_ratio_extreme import LSRatioExtremeSetup
from .multi_tf_trend import MultiTFTrendSetup
from .oi_divergence import OIDivergenceSetup
from .order_block import OrderBlockSetup
from .price_velocity import PriceVelocitySetup
from .rsi_divergence_bottom import RSIDivergenceBottomSetup
from .session_killzone import SessionKillzoneSetup
from .spread_strategy import SpreadStrategySetup
from .squeeze_setup import SqueezeSetup
from .stop_hunt_detection import StopHuntDetectionSetup
from .structure_break_retest import StructureBreakRetestSetup
from .structure_pullback import StructurePullbackSetup
from .supertrend_follow import SuperTrendFollowSetup
from .turtle_soup import TurtleSoupSetup
from .volume_anomaly import VolumeAnomalySetup
from .volume_climax_reversal import VolumeClimaxReversalSetup
from .vwap_trend import VWAPTrendSetup
from .whale_walls import WhaleWallsSetup
from .wick_trap_reversal import WickTrapReversalSetup
from .wyckoff_spring import WyckoffSpringSetup

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
    IndicatorDivergenceSetup,
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

__all__ = [
    "STRATEGY_CLASSES",
    "ATRExpansionSetup",
    "AbsorptionSetup",
    "AggressionShiftSetup",
    "AltcoinSeasonIndexSetup",
    "BBSqueezeSetup",
    "BOSCHOCHSetup",
    "BTCCorrelationSetup",
    "BreakerBlockSetup",
    "CVDDivergenceSetup",
    "DepthImbalanceSetup",
    "EmaBounceSetup",
    "FVGSetup",
    "FundingReversalSetup",
    "HiddenDivergenceSetup",
    "IndicatorDivergenceSetup",
    "KeltnerBreakoutSetup",
    "LSRatioExtremeSetup",
    "LiquidationHeatmapSetup",
    "LiquiditySweepSetup",
    "MultiTFTrendSetup",
    "OIDivergenceSetup",
    "OrderBlockSetup",
    "PriceVelocitySetup",
    "RSIDivergenceBottomSetup",
    "SessionKillzoneSetup",
    "SpreadStrategySetup",
    "SqueezeSetup",
    "StopHuntDetectionSetup",
    "StructureBreakRetestSetup",
    "StructurePullbackSetup",
    "SuperTrendFollowSetup",
    "TurtleSoupSetup",
    "VWAPTrendSetup",
    "VolumeAnomalySetup",
    "VolumeClimaxReversalSetup",
    "WhaleWallsSetup",
    "WickTrapReversalSetup",
    "WyckoffSpringSetup",
]

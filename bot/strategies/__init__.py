"""Modern strategy exports."""

from .absorption import AbsorptionSetup
from .altcoin_season_index import AltcoinSeasonIndexSetup
from .bos_choch import BOSCHOCHSetup
from .btc_correlation import BTCCorrelationSetup
from .cvd_divergence import CVDDivergenceSetup
from .ema_bounce import EmaBounceSetup
from .funding_reversal import FundingReversalSetup
from .fvg import FVGSetup
from .indicator_divergence import IndicatorDivergenceSetup
from .keltner_breakout import KeltnerBreakoutSetup
from .liquidation_heatmap import LiquidationHeatmapSetup
from .liquidity_sweep import LiquiditySweepSetup
from .ls_ratio_extreme import LSRatioExtremeSetup
from .multi_tf_trend import MultiTFTrendSetup
from .oi_divergence import OIDivergenceSetup
from .order_block import OrderBlockSetup
from .pinbar_reversal import PinbarReversalSetup
from .price_velocity import PriceVelocitySetup
from .squeeze_setup import SqueezeSetup
from .structure_break_retest import StructureBreakRetestSetup
from .structure_pullback import StructurePullbackSetup
from .supertrend_follow import SuperTrendFollowSetup
from .turtle_soup import TurtleSoupSetup
from .volume_anomaly import VolumeAnomalySetup
from .volume_climax_reversal import VolumeClimaxReversalSetup
from .vwap_trend import VWAPTrendSetup
from .wick_trap_reversal import WickTrapReversalSetup
from .wyckoff_spring import WyckoffSpringSetup

# 28 canonical setups (full answers.md merge):
# liquidity_sweep ← fakeout, stop_hunt | cvd_divergence ← cvd_exhaustion
# squeeze_setup ← bb_squeeze, atr_expansion | order_block ← breaker_block
# indicator_divergence ← hidden_divergence, rsi_divergence_bottom
# confluence legs ← session_killzone, orderflow, aggression, depth_imbalance
# removed Evidence C ← whale_walls, spread_strategy
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
    FundingReversalSetup,
    IndicatorDivergenceSetup,
    KeltnerBreakoutSetup,
    CVDDivergenceSetup,
    TurtleSoupSetup,
    VWAPTrendSetup,
    SuperTrendFollowSetup,
    PriceVelocitySetup,
    VolumeAnomalySetup,
    VolumeClimaxReversalSetup,
    AbsorptionSetup,
    LiquidationHeatmapSetup,
    MultiTFTrendSetup,
    WyckoffSpringSetup,
    LSRatioExtremeSetup,
    OIDivergenceSetup,
    BTCCorrelationSetup,
    AltcoinSeasonIndexSetup,
    PinbarReversalSetup,
)

__all__ = [
    "STRATEGY_CLASSES",
    "AbsorptionSetup",
    "AltcoinSeasonIndexSetup",
    "BOSCHOCHSetup",
    "BTCCorrelationSetup",
    "CVDDivergenceSetup",
    "EmaBounceSetup",
    "FVGSetup",
    "FundingReversalSetup",
    "IndicatorDivergenceSetup",
    "KeltnerBreakoutSetup",
    "LSRatioExtremeSetup",
    "LiquidationHeatmapSetup",
    "LiquiditySweepSetup",
    "MultiTFTrendSetup",
    "OIDivergenceSetup",
    "OrderBlockSetup",
    "PinbarReversalSetup",
    "PriceVelocitySetup",
    "SqueezeSetup",
    "StructureBreakRetestSetup",
    "StructurePullbackSetup",
    "SuperTrendFollowSetup",
    "TurtleSoupSetup",
    "VWAPTrendSetup",
    "VolumeAnomalySetup",
    "VolumeClimaxReversalSetup",
    "WickTrapReversalSetup",
    "WyckoffSpringSetup",
]

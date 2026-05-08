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

STRATEGY_STATUS_BY_ID = {
    "structure_pullback": "beta",
    "structure_break_retest": "beta",
    "wick_trap_reversal": "beta",
    "squeeze_setup": "beta",
    "ema_bounce": "production",
    "fvg_setup": "production",
    "order_block": "beta",
    "liquidity_sweep": "beta",
    "bos_choch": "beta",
    "hidden_divergence": "beta",
    "funding_reversal": "experimental",
    "cvd_divergence": "beta",
    "session_killzone": "experimental",
    "breaker_block": "beta",
    "turtle_soup": "production",
    "vwap_trend": "beta",
    "supertrend_follow": "beta",
    "price_velocity": "beta",
    "volume_anomaly": "beta",
    "volume_climax_reversal": "beta",
    "keltner_breakout": "production",
    "whale_walls": "experimental",
    "spread_strategy": "experimental",
    "depth_imbalance": "experimental",
    "absorption": "experimental",
    "aggression_shift": "experimental",
    "liquidation_heatmap": "production",
    "stop_hunt_detection": "beta",
    "multi_tf_trend": "production",
    "rsi_divergence_bottom": "experimental",
    "wyckoff_spring": "beta",
    "bb_squeeze": "production",
    "atr_expansion": "experimental",
    "ls_ratio_extreme": "beta",
    "oi_divergence": "experimental",
    "btc_correlation": "experimental",
    "altcoin_season_index": "experimental",
}

RISK_PROFILE_BY_ID = {
    "spread_strategy": "microstructure",
    "whale_walls": "microstructure",
    "depth_imbalance": "microstructure",
    "absorption": "microstructure",
    "aggression_shift": "microstructure",
    "funding_reversal": "sentiment",
    "ls_ratio_extreme": "sentiment",
    "oi_divergence": "sentiment",
    "btc_correlation": "multi_asset",
    "altcoin_season_index": "multi_asset",
}

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
    _setup_id = _strategy_class.setup_id
    _strategy_class.asset_fit = ASSET_FIT_PROFILES[_setup_id]
    _strategy_class.status = STRATEGY_STATUS_BY_ID.get(_setup_id, "beta")
    _strategy_class.risk_profile = RISK_PROFILE_BY_ID.get(
        _setup_id, getattr(_strategy_class, "family", "generic")
    )

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
    "STRATEGY_STATUS_BY_ID",
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

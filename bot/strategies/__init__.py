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
)

__all__ = [
    "BOSCHOCHSetup",
    "BreakerBlockSetup",
    "CVDDivergenceSetup",
    "EmaBounceSetup",
    "FundingReversalSetup",
    "FVGSetup",
    "HiddenDivergenceSetup",
    "KeltnerBreakoutSetup",
    "LiquiditySweepSetup",
    "OrderBlockSetup",
    "PriceVelocitySetup",
    "SessionKillzoneSetup",
    "SqueezeSetup",
    "STRATEGY_CLASSES",
    "StructureBreakRetestSetup",
    "StructurePullbackSetup",
    "SuperTrendFollowSetup",
    "TurtleSoupSetup",
    "VolumeAnomalySetup",
    "VolumeClimaxReversalSetup",
    "VWAPTrendSetup",
    "WickTrapReversalSetup",
]

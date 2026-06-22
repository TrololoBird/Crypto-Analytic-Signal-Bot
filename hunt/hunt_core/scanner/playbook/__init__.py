"""Scanner playbook — re-export from analysis (Phase 2 consolidation)."""
from hunt_core.analysis.archetypes import canonical_archetype  # noqa: F401
from hunt_core.analysis.manipulation_fusion import (  # noqa: F401
    evaluate_manipulation_fusion,
    squeeze_blocks_predump_short,
    stamp_fusion_on_row,
)
from hunt_core.shared.facts.adx_thresholds import (  # noqa: F401
    ADX_MEME_RANGE_MAX,
    ADX_MEME_TREND_MIN,
    ADX_RANGE_MAX,
    ADX_STRONG_MIN,
    ADX_TREND_MIN,
)
from hunt_core.shared.facts.trend import normalize_rsi14, trend_1h_bias  # noqa: F401
from hunt_core.analysis.playbook_checks import PLAYBOOK_N_OF_M, PLAYBOOK_REQUIRED  # noqa: F401
from hunt_core.analysis.playbook_eval import setup_meets_playbook  # noqa: F401

__all__ = [
    "ADX_MEME_RANGE_MAX",
    "ADX_MEME_TREND_MIN",
    "ADX_RANGE_MAX",
    "ADX_STRONG_MIN",
    "ADX_TREND_MIN",
    "PLAYBOOK_N_OF_M",
    "PLAYBOOK_REQUIRED",
    "canonical_archetype",
    "normalize_rsi14",
    "setup_meets_playbook",
    "squeeze_blocks_predump_short",
    "stamp_fusion_on_row",
    "trend_1h_bias",
]

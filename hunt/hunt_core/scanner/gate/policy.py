"""Edge, MTF, and regime ensemble policy gates — public façade."""
from __future__ import annotations

from hunt_core.scanner.gate._policy_decl import *  # noqa: F403
from hunt_core.scanner.gate._policy_edge import *  # noqa: F403
from hunt_core.scanner.gate._policy_mtf import *  # noqa: F403
from hunt_core.scanner.gate._policy_regime import *  # noqa: F403

# Re-export ADX constants used by legacy imports.
from hunt_core.scanner.playbook import (  # noqa: F401
    ADX_MEME_RANGE_MAX,
    ADX_MEME_TREND_MIN,
    ADX_RANGE_MAX,
    ADX_TREND_MIN,
)

ADX_TREND = ADX_TREND_MIN
ADX_RANGE = ADX_RANGE_MAX

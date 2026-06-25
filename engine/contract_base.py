"""Shared contract types and constants — canonical source for both engine/ and hunt/.

engine/contract.py re-exports from this module.
hunt/hunt_core/contract.py should be synced from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

# ---------------------------------------------------------------------------
# Constants — single source of truth
# ---------------------------------------------------------------------------

DEFAULT_SCALE_WEIGHTS: tuple[float, float, float] = (0.5, 0.3, 0.2)
DEFAULT_TARGET_RR: tuple[float, float, float] = (1.9, 3.0, 5.0)
DEFAULT_MIN_RISK_REWARD = 1.9
DEFAULT_MAX_RISK_REWARD = 10.0
RISK_REWARD_EPSILON = 1e-9

# Entry-zone width in ATR multiples
SIGNAL_ENTRY_PAD_ATR: float = 0.35

# ---------------------------------------------------------------------------
# Timeframe helpers
# ---------------------------------------------------------------------------

TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "1d": 1440,
}

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradePlan:
    direction: str
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    valid_until: datetime
    scale_weights: tuple[float, float, float]
    ttl_bars: int
    entry_zone_width_pct: float
    risk_reward_tp1: float
    risk_reward_tp2: float
    risk_reward_tp3: float
    single_target_mode: bool
    integrity_status: str

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2.0

    @property
    def entry_zone(self) -> tuple[float, float]:
        return (self.entry_low, self.entry_high)


@dataclass(frozen=True, slots=True)
class SignalContractIssue:
    field: str
    reason: str
    value: object = None

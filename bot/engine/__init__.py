"""Core engine for pluggable strategy system."""

from ..domain.strategies import StrategyDecision
from .base import AbstractStrategy, SignalResult
from .engine import SignalEngine
from .lanes import select_lane_setups
from .registry import StrategyMetadata, StrategyRegistry

__all__ = [
    "AbstractStrategy",
    "SignalEngine",
    "SignalResult",
    "StrategyDecision",
    "StrategyMetadata",
    "StrategyRegistry",
    "select_lane_setups",
]

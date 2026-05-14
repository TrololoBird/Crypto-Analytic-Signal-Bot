"""Abstract base class for all strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


from ...domain.schemas import PreparedSymbol
from ...domain.strategies import (
    StrategyMetadata,
    SignalResult,
    StrategyDecision as StrategyDecision,
)

__all__ = ["AbstractStrategy", "SignalResult", "StrategyDecision", "StrategyMetadata"]


def _has_oi_context(prepared: PreparedSymbol) -> bool:
    return prepared.oi_current is not None or prepared.oi_change_pct is not None


class AbstractStrategy(ABC):
    """Abstract base class for all trading strategies.

    All strategies must inherit from this class and implement:
    - metadata property
    - calculate() method
    - can_calculate() method
    """

    def __init__(self, settings: Any = None):
        self._settings = settings
        self._parameters: dict[str, Any] = {}

    @property
    @abstractmethod
    def metadata(self) -> StrategyMetadata:
        """Return strategy metadata for registration."""
        pass

    @abstractmethod
    def calculate(self, prepared: PreparedSymbol) -> SignalResult:
        """Calculate signal for given prepared symbol data.

        Args:
            prepared: Prepared symbol data with indicators

        Returns:
            SignalResult with signal or None if no setup
        """
        pass

    def can_calculate(self, prepared: PreparedSymbol) -> bool:
        """Check if strategy can calculate with available data.

        Override for custom validation (OI data, funding, etc.)
        """
        if prepared.work_1h is None or prepared.work_1h.is_empty():
            return False

        if prepared.work_1h.height < self.metadata.min_history_bars:
            return False

        if self.metadata.requires_oi and not _has_oi_context(prepared):
            return False

        if self.metadata.requires_funding and prepared.funding_rate is None:
            return False

        return True

    def update_parameters(self, parameters: dict[str, Any]) -> None:
        """Hot-update strategy parameters from optimizer."""
        self._parameters.update(parameters)

    def get_parameter(self, name: str, default: Any = None) -> Any:
        """Get parameter value with default."""
        return self._parameters.get(name, default)

    @property
    def strategy_id(self) -> str:
        return self.metadata.strategy_id

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.strategy_id}>"

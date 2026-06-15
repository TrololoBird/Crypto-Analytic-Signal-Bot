"""Abstract base class for all strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from engine.domain.strategies import (
    SignalResult,
    StrategyDecision,
    StrategyMetadata,
)

if TYPE_CHECKING:
    from engine.domain.schemas import PreparedSymbol

__all__ = ["AbstractStrategy", "SignalResult", "StrategyDecision", "StrategyMetadata"]


def _has_oi_context(prepared: PreparedSymbol) -> bool:
    return prepared.oi_current is not None or prepared.oi_change_pct is not None


def _missing_required_features(
    prepared: PreparedSymbol, features: tuple[str, ...]
) -> tuple[str, ...]:
    if not features:
        return ()
    columns: set[str] = set()
    for frame_name in ("work_15m", "work_1h", "work_4h"):
        frame = getattr(prepared, frame_name, None)
        if frame is not None:
            columns.update(frame.columns)
    return tuple(feature for feature in features if feature not in columns)


def _missing_required_enrichment(
    prepared: PreparedSymbol, enrichment_fields: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(field for field in enrichment_fields if getattr(prepared, field, None) is None)


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

    @abstractmethod
    def calculate(self, prepared: PreparedSymbol) -> SignalResult:
        """Calculate signal for given prepared symbol data.

        Args:
            prepared: Prepared symbol data with indicators

        Returns:
            SignalResult with signal or None if no setup
        """

    def can_calculate(self, prepared: PreparedSymbol) -> bool:
        """Check if strategy can calculate with available data.

        Override for custom validation (OI data, funding, etc.)
        """
        metadata = self.metadata

        if prepared.work_1h is None or prepared.work_1h.is_empty():
            return False

        if prepared.work_1h.height < metadata.min_history_bars:
            return False

        if metadata.requires_oi and not _has_oi_context(prepared):
            return False

        if metadata.requires_funding and prepared.funding_rate is None:
            return False

        if _missing_required_features(prepared, tuple(metadata.required_features or ())):
            return False

        return not _missing_required_enrichment(prepared, tuple(metadata.required_enrichment or ()))

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

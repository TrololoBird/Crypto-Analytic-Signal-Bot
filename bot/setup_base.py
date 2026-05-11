"""Base classes for all trading setup detectors."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from .domain.config import BotSettings
from .domain.schemas import PreparedSymbol, Signal
from .domain.strategies import STRATEGY_STATUS_BY_ID, RISK_PROFILE_BY_ID
from .core.engine.base import (
    AbstractStrategy,
    SignalResult,
    StrategyDecision,
    StrategyMetadata,
)
from .core.runtime_errors import classify_runtime_error
from .strategy_asset_fit import (
    ASSET_FIT_PROFILES,
    DEFAULT_ASSET_FIT,
    AssetFit,
    asset_fit_reject_reason,
    market_context_from_prepared,
)
from .setups import (
    begin_strategy_decision_capture,
    finalize_strategy_decision,
    reset_strategy_decision_capture,
)


@dataclass(frozen=True)
class SetupParams:
    """Per-setup configuration parameters."""

    enabled: bool = True


class BaseSetup(AbstractStrategy):
    """Base setup class compatible with the modern signal engine."""

    setup_id: str  # class-level constant, defined by each subclass
    family: str = "continuation"
    confirmation_profile: str = "trend_follow"
    required_context: tuple[str, ...] = ()
    required_features: tuple[str, ...] = ()
    required_enrichment: tuple[str, ...] = ()
    requires_oi: bool = False
    requires_funding: bool = False
    min_history_bars: int = 50
    score_calibration: str = "heuristic"

    def __init__(
        self, params: SetupParams | None = None, settings: BotSettings | None = None
    ) -> None:
        super().__init__(settings)
        self.params = params or SetupParams()

    def is_enabled(self) -> bool:
        return self.params.enabled

    @property
    def asset_fit(self) -> AssetFit:
        return ASSET_FIT_PROFILES.get(self.setup_id, DEFAULT_ASSET_FIT)

    @property
    def status(self) -> str:
        return STRATEGY_STATUS_BY_ID.get(self.setup_id, "beta")

    @property
    def risk_profile(self) -> str:
        return RISK_PROFILE_BY_ID.get(self.setup_id, self.family)

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id=self.setup_id,
            name=self.setup_id.replace("_", " ").title(),
            description=f"{self.setup_id} setup",
            status=self.status,
            family=self.family,
            confirmation_profile=self.confirmation_profile,
            required_context=self.required_context,
            required_features=self.required_features,
            required_enrichment=self.required_enrichment,
            requires_oi=bool(self.requires_oi or self.asset_fit.requires_oi),
            requires_funding=bool(
                self.requires_funding or self.asset_fit.requires_funding
            ),
            min_history_bars=self.min_history_bars,
            asset_fit=self.asset_fit.to_dict(),
            score_calibration=self.score_calibration,
            risk_profile=self.risk_profile,
        )

    @abstractmethod
    def detect(
        self,
        prepared: PreparedSymbol,
        settings: BotSettings,
    ) -> StrategyDecision | Signal | None:
        """Run detection logic."""
        ...

    @abstractmethod
    def get_optimizable_params(
        self, settings: "BotSettings | None" = None
    ) -> dict[str, float]:
        """Return tunable parameters for self-learner Optuna optimization."""
        ...

    def calculate(self, prepared: PreparedSymbol) -> SignalResult:
        if self._settings is None:
            decision = StrategyDecision.error_result(
                setup_id=self.setup_id,
                reason_code="runtime.missing_settings",
                error=f"{self.setup_id} missing BotSettings",
                details={"symbol": prepared.symbol},
            )
            return SignalResult(
                setup_id=self.setup_id,
                signal=None,
                decision=decision,
                error=decision.error,
                metadata={"setup_id": self.setup_id},
            )
        runtime = getattr(self._settings, "runtime", None)
        strict_data_quality = bool(getattr(runtime, "strict_data_quality", True))
        token = begin_strategy_decision_capture(
            prepared=prepared,
            setup_id=self.setup_id,
            strict_data_quality=strict_data_quality,
        )
        try:
            try:
                outcome = self.detect(prepared, self._settings)
            except Exception as exc:
                error_class = classify_runtime_error(exc)
                decision = StrategyDecision.error_result(
                    setup_id=self.setup_id,
                    reason_code=f"{error_class}.error",
                    error=str(exc),
                    stage="engine",
                    details={
                        "symbol": prepared.symbol,
                        "error_class": error_class,
                        "exception_type": type(exc).__name__,
                    },
                )
            else:
                decision = finalize_strategy_decision(
                    prepared=prepared,
                    setup_id=self.setup_id,
                    outcome=outcome,
                )
        finally:
            reset_strategy_decision_capture(token)
        return SignalResult(
            setup_id=self.setup_id,
            signal=decision.signal,
            decision=decision,
            error=decision.error,
            metadata={"setup_id": self.setup_id},
        )

    def can_calculate(self, prepared: PreparedSymbol) -> bool:
        if not self.is_enabled():
            return False
        reason = asset_fit_reject_reason(
            self.setup_id,
            prepared.symbol,
            market_context_from_prepared(prepared),
            settings=self._settings,
        )
        if reason is not None:
            return False
        return super().can_calculate(prepared)

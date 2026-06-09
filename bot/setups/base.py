"""Base classes for all trading setup detectors."""

from __future__ import annotations

import dataclasses
import logging
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from bot.runtime.errors import DEFENSIVE_EXC, classify_runtime_error

from ..domain.schemas import is_signal_contract_violation
from ..domain.strategies import RISK_PROFILE_BY_ID, STRATEGY_STATUS_BY_ID, StrategyDecision
from ..domain.strategy_catalog import CATALOG_BY_ID
from ..engine.base import (
    AbstractStrategy,
    SignalResult,
    StrategyMetadata,
)
from ..market.fit import (
    ASSET_FIT_PROFILES,
    DEFAULT_ASSET_FIT,
    AssetFit,
    asset_fit_reject_reason,
    market_context_from_prepared,
)
from . import (
    _reject,
    begin_strategy_decision_capture,
    finalize_strategy_decision,
    reset_strategy_decision_capture,
)

LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal


@dataclass(frozen=True)
class SetupParams:
    """Per-setup configuration parameters."""

    enabled: bool = True


class BaseSetup(AbstractStrategy):
    """Base setup class compatible with the modern signal engine."""

    setup_id: str  # class-level constant, defined by each subclass
    ENTRY_ORDER_TYPE: ClassVar[str] = "limit"
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
        catalog = CATALOG_BY_ID.get(self.setup_id)
        trigger_tf = catalog.trigger_tf if catalog is not None else "15m"
        trigger_intervals = catalog.trigger_intervals if catalog is not None else ()
        pattern_tf = catalog.pattern_tf if catalog is not None else "15m"
        required_tfs = catalog.required_tfs if catalog is not None else (trigger_tf,)
        evidence_level = catalog.evidence_level if catalog is not None else "A"
        # Trigger routing only - required_tfs are for data/WS union, not lane fallback.
        timeframes = list(dict.fromkeys((trigger_tf, *trigger_intervals)))
        return StrategyMetadata(
            strategy_id=self.setup_id,
            name=self.setup_id.replace("_", " ").title(),
            description=f"{self.setup_id} setup",
            status=self.status,
            tags=[],
            timeframes=list(timeframes),
            family=self.family,
            confirmation_profile=self.confirmation_profile,
            trigger_tf=trigger_tf,
            trigger_intervals=trigger_intervals,
            pattern_tf=pattern_tf,
            required_tfs=required_tfs,
            evidence_level=evidence_level,
            required_context=self.required_context,
            required_features=self.required_features,
            required_enrichment=self.required_enrichment,
            requires_oi=bool(self.requires_oi or self.asset_fit.requires_oi),
            requires_funding=bool(self.requires_funding or self.asset_fit.requires_funding),
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

    def get_optimizable_params(self, _settings: BotSettings | None = None) -> dict[str, float]:
        """Return tunable parameters. Override in subclass to enable autotuning."""
        return {}

    def _schedule_active(self, prepared: PreparedSymbol) -> bool:
        checker = getattr(self, "is_active_now", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(prepared, self._settings))
        except TypeError:
            return bool(checker(prepared))
        except DEFENSIVE_EXC:
            LOG.exception(
                "%s: strategy schedule check failed | strategy=%s",
                prepared.symbol,
                self.setup_id,
            )
            return False

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
            if not self._schedule_active(prepared):
                _reject(
                    prepared,
                    self.setup_id,
                    "schedule_inactive",
                    stage="context",
                    symbol=prepared.symbol,
                )
                decision = finalize_strategy_decision(
                    prepared=prepared,
                    setup_id=self.setup_id,
                    outcome=None,
                )
            else:
                try:
                    outcome = self.detect(prepared, self._settings)
                except DEFENSIVE_EXC as exc:
                    if is_signal_contract_violation(exc):
                        _reject(
                            prepared,
                            self.setup_id,
                            "targets.contract_violation",
                            stage="runtime",
                            detail=str(exc),
                        )
                        decision = finalize_strategy_decision(
                            prepared=prepared,
                            setup_id=self.setup_id,
                            outcome=None,
                        )
                    else:
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
        sig = decision.signal
        from bot.domain.strategy_catalog import resolve_setup_order_type

        expected_ot = resolve_setup_order_type(
            self.setup_id,
            default=str(type(self).ENTRY_ORDER_TYPE or "limit"),
        ).strip().lower()
        if sig is not None and sig.entry_order_type != expected_ot:
            sig = dataclasses.replace(sig, entry_order_type=expected_ot)
        result = SignalResult(
            setup_id=self.setup_id,
            signal=sig,
            decision=decision,
            error=decision.error,
            metadata={"setup_id": self.setup_id},
        )
        return result

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

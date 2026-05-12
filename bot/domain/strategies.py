from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .schemas import Signal


@dataclass(frozen=True, slots=True)
class StrategyMetadata:
    """Metadata for strategy registration."""

    strategy_id: str
    name: str
    description: str = ""
    version: str = "1.0.0"
    author: str = ""
    status: str = "beta"
    tags: list[str] = field(default_factory=list)
    timeframes: list[str] = field(default_factory=lambda: ["5m", "15m", "1h"])
    family: str = "continuation"
    confirmation_profile: str = "trend_follow"
    required_context: tuple[str, ...] = ()
    required_features: tuple[str, ...] = ()
    required_enrichment: tuple[str, ...] = ()
    requires_oi: bool = False  # Requires open interest data
    requires_funding: bool = False  # Requires funding rate data
    min_history_bars: int = 50  # Minimum bars needed for calculation
    asset_fit: dict[str, object] = field(default_factory=dict)
    score_calibration: str = "heuristic"
    risk_profile: str = "generic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "status": self.status,
            "tags": self.tags,
            "timeframes": self.timeframes,
            "family": self.family,
            "confirmation_profile": self.confirmation_profile,
            "required_context": list(self.required_context),
            "required_features": list(self.required_features),
            "required_enrichment": list(self.required_enrichment),
            "requires_oi": self.requires_oi,
            "requires_funding": self.requires_funding,
            "min_history_bars": self.min_history_bars,
            "asset_fit": self.asset_fit,
            "score_calibration": self.score_calibration,
            "risk_profile": self.risk_profile,
        }


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """Normalized strategy output used across engine, bot, and telemetry."""

    setup_id: str
    status: str
    stage: str
    reason_code: str
    signal: Signal | None = None
    details: dict[str, Any] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    invalid_fields: tuple[str, ...] = ()
    error: str | None = None

    @property
    def is_signal(self) -> bool:
        return self.status == "signal" and self.signal is not None and self.error is None

    @property
    def is_reject(self) -> bool:
        return self.status == "reject"

    @property
    def is_error(self) -> bool:
        return self.status == "error" or self.error is not None

    @property
    def is_skip(self) -> bool:
        return self.status == "skip"

    @classmethod
    def signal_hit(
        cls,
        *,
        setup_id: str,
        signal: Signal,
        stage: str = "strategy",
        reason_code: str = "pattern.raw_hit",
        details: dict[str, Any] | None = None,
    ) -> "StrategyDecision":
        return cls(
            setup_id=setup_id,
            status="signal",
            stage=stage,
            reason_code=reason_code,
            signal=signal,
            details=dict(details or {}),
        )

    @classmethod
    def reject(
        cls,
        *,
        setup_id: str,
        stage: str,
        reason_code: str,
        details: dict[str, Any] | None = None,
        missing_fields: tuple[str, ...] = (),
        invalid_fields: tuple[str, ...] = (),
    ) -> "StrategyDecision":
        return cls(
            setup_id=setup_id,
            status="reject",
            stage=stage,
            reason_code=reason_code,
            details=dict(details or {}),
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
        )

    @classmethod
    def skip(
        cls,
        *,
        setup_id: str,
        reason_code: str,
        details: dict[str, Any] | None = None,
        missing_fields: tuple[str, ...] = (),
        invalid_fields: tuple[str, ...] = (),
    ) -> "StrategyDecision":
        return cls(
            setup_id=setup_id,
            status="skip",
            stage="engine",
            reason_code=reason_code,
            details=dict(details or {}),
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
        )

    @classmethod
    def error_result(
        cls,
        *,
        setup_id: str,
        reason_code: str,
        error: str,
        stage: str = "runtime",
        details: dict[str, Any] | None = None,
    ) -> "StrategyDecision":
        return cls(
            setup_id=setup_id,
            status="error",
            stage=stage,
            reason_code=reason_code,
            details=dict(details or {}),
            error=error,
        )


@dataclass
class SignalResult:
    """Result from strategy calculation."""

    setup_id: str
    signal: Signal | None
    decision: StrategyDecision | None = None
    confidence: float = 0.0  # 0.0-1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    calculation_time_ms: float = 0.0
    error: str | None = None

    def __post_init__(self) -> None:
        if "setup_id" not in self.metadata:
            self.metadata["setup_id"] = self.setup_id
        if self.decision is None:
            if self.error is not None:
                self.decision = StrategyDecision.error_result(
                    setup_id=self.setup_id,
                    reason_code="runtime.error",
                    error=self.error,
                    details=dict(self.metadata),
                )
            elif self.signal is not None:
                self.decision = StrategyDecision.signal_hit(
                    setup_id=self.setup_id,
                    signal=self.signal,
                    details=dict(self.metadata),
                )
            else:
                self.decision = StrategyDecision.reject(
                    setup_id=self.setup_id,
                    stage="strategy",
                    reason_code="pattern.no_raw_hit",
                    details=dict(self.metadata),
                )
        if self.error is None and self.decision is not None and self.decision.error is not None:
            self.error = self.decision.error

    @property
    def is_valid(self) -> bool:
        return self.decision is not None and self.decision.is_signal


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

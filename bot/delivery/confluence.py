"""ConfluenceEngine — unified signal quality scoring."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..features.microstructure import build_microstructure_context
from .scoring import (
    ScoringResult,
    _crowd_position,
    _funding_contrarian,
    _mtf_alignment,
    _oi_momentum,
    _risk_reward_quality,
    _structure_clarity,
    _volume_quality,
)

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.confluence")
MIN_HISTORY_SAMPLES = 20


@dataclass(frozen=True, slots=True)
class ComponentScore:
    """Score contribution from a single factor."""

    name: str
    raw: float
    weight: float
    contribution: float
    available: bool = True


@dataclass(frozen=True)
class ConfluenceResult:
    """Full quality assessment of a signal."""

    setup_id: str
    setup_prior: float
    components: tuple[ComponentScore, ...]
    final_score: float

    @property
    def weighted_model_score(self) -> float:
        return sum(c.contribution for c in self.components)

    @property
    def weight_sum_actual(self) -> float:
        return round(sum(c.weight for c in self.components if c.available and c.weight > 0.0), 6)

    def to_scoring_result(self) -> ScoringResult:
        adjustments = {c.name: c.contribution for c in self.components}
        return ScoringResult(
            base_score=self.setup_prior,
            adjustments=adjustments,
            final_score=self.final_score,
            setup_id=self.setup_id,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "setup_prior": self.setup_prior,
            "setup_id": self.setup_id,
            "components": [
                {
                    "name": c.name,
                    "raw": c.raw,
                    "weight": c.weight,
                    "contribution": c.contribution,
                    "available": c.available,
                }
                for c in self.components
            ],
            "weighted_model_score": self.weighted_model_score,
            "weight_sum_actual": self.weight_sum_actual,
            "final_score": self.final_score,
        }


class ConfluenceEngine:
    """Single entry point for signal quality assessment.

    Usage::

        engine = ConfluenceEngine(settings)
        result = engine.score(signal, prepared)
    """

    def __init__(self, settings: BotSettings) -> None:
        self.settings = settings

    def score(self, signal: Signal, prepared: PreparedSymbol) -> ConfluenceResult:
        cfg = self.settings.scoring
        components = self._compute_components(signal, prepared, cfg)
        model_score = sum(c.contribution for c in components)

        prior_w = max(0.0, min(cfg.setup_prior_weight, 1.0))
        history_count = int(
            getattr(
                signal,
                "setup_history_count",
                getattr(signal, "history_count", 0),
            )
            or 0
        )
        calibrated_prior = self._calibrate_setup_prior(signal.score, history_count=history_count)
        calibrated_model = self._calibrate_component_model(model_score)
        blended = (calibrated_prior * prior_w) + (calibrated_model * (1.0 - prior_w))
        final = round(
            self._apply_component_edge(blended, signal=signal, components=components),
            4,
        )

        return ConfluenceResult(
            setup_id=signal.setup_id,
            setup_prior=signal.score,
            components=tuple(components),
            final_score=final,
        )

    def _compute_components(
        self,
        signal: Signal,
        prepared: PreparedSymbol,
        cfg: Any,
    ) -> list[ComponentScore]:
        funding_weight = max(0.0, min(cfg.weight_crowd_position * 0.5, cfg.weight_crowd_position))
        crowd_weight = max(0.0, cfg.weight_crowd_position - funding_weight)
        micro_context = self._microstructure_context(prepared, signal)
        micro_available = micro_context.confidence >= 0.35
        micro_raw = 0.5 + (micro_context.bias_score * 0.5)
        raw_specs = [
            {
                "name": "mtf_alignment",
                "weight": cfg.weight_mtf_alignment,
                "raw": _mtf_alignment(prepared, signal),
                "available": True,
            },
            {
                "name": "volume_quality",
                "weight": cfg.weight_volume_quality,
                "raw": _volume_quality(prepared),
                "available": self._has_latest_feature(prepared, "work_15m", "volume_ratio20"),
            },
            {
                "name": "structure_clarity",
                "weight": cfg.weight_structure_clarity,
                "raw": _structure_clarity(prepared, signal),
                "available": self._has_structure_context(prepared),
            },
            {
                "name": "risk_reward",
                "weight": cfg.weight_risk_reward,
                "raw": _risk_reward_quality(signal, self.settings),
                "available": bool(signal.risk_reward and signal.risk_reward > 0.0),
            },
            {
                "name": "funding_score",
                "weight": funding_weight,
                "raw": _funding_contrarian(prepared, signal, self.settings),
                "available": self._has_finite_attr(prepared, "funding_rate"),
            },
            {
                "name": "crowd_position",
                "weight": crowd_weight,
                "raw": _crowd_position(prepared, signal, self.settings),
                "available": self._has_crowding_context(prepared),
            },
            {
                "name": "oi_momentum",
                "weight": cfg.weight_oi_momentum,
                "raw": _oi_momentum(prepared, signal),
                "available": self._has_oi_or_flow_context(prepared, signal),
            },
            {
                "name": "microstructure_context",
                "weight": max(float(cfg.weight_oi_momentum) * 0.85, 0.07),
                "raw": micro_raw,
                "available": micro_available,
            },
        ]
        specs: list[dict[str, Any]] = []
        for spec in raw_specs:
            base_weight = max(0.0, float(spec["weight"]))
            if not spec["available"]:
                base_weight = 0.0
            adjusted_weight = base_weight * self._component_family_multiplier(
                str(spec["name"]),
                signal,
            )
            specs.append({**spec, "weight": adjusted_weight})

        weight_total = sum(max(0.0, float(spec["weight"])) for spec in specs)
        if weight_total > 0.0:
            specs = [
                {**spec, "weight": max(0.0, float(spec["weight"])) / weight_total} for spec in specs
            ]
        actual_sum = sum(max(0.0, float(spec["weight"])) for spec in specs)
        if weight_total > 0.0 and abs(actual_sum - 1.0) > 0.01:
            LOG.warning(
                "ConfluenceEngine weight normalization drift | sum=%.6f setup_id=%s",
                actual_sum,
                signal.setup_id,
            )
        for spec in specs:
            weight = max(0.0, float(spec["weight"]))
            if bool(spec["available"]) and 0.0 < weight < 0.01:
                LOG.warning(
                    "ConfluenceEngine tiny component weight | component=%s weight=%.6f setup_id=%s",
                    spec["name"],
                    weight,
                    signal.setup_id,
                )
            if weight > 0.70:
                LOG.warning(
                    (
                        "ConfluenceEngine dominant component weight | component=%s "
                        "weight=%.6f setup_id=%s"
                    ),
                    spec["name"],
                    weight,
                    signal.setup_id,
                )
        if __debug__:
            active_weight_sum = sum(
                max(0.0, float(spec["weight"]))
                for spec in specs
                if bool(spec["available"]) and float(spec["weight"]) > 0.0
            )
            if weight_total > 0.0:
                assert abs(active_weight_sum - 1.0) < 1e-9, active_weight_sum
        return [self._component_from_spec(spec) for spec in specs]

    @staticmethod
    def _component_from_spec(spec: dict[str, Any]) -> ComponentScore:
        raw = max(0.0, min(float(spec["raw"]), 1.0))
        weight = max(0.0, float(spec["weight"]))
        return ComponentScore(
            name=str(spec["name"]),
            raw=round(raw, 4),
            weight=round(weight, 4),
            contribution=round(weight * raw, 4),
            available=bool(spec["available"]),
        )

    @staticmethod
    def _soft_clip_score(score: float, *, strength: float) -> float:
        """Monotone score calibration that avoids hard saturation at 0 or 1."""
        numeric = max(0.0, min(float(score), 1.0))
        if numeric <= 0.0 or numeric >= 1.0:
            return numeric
        low = 1.0 / (1.0 + math.exp(strength * 5.0))
        high = 1.0 / (1.0 + math.exp(-strength * 5.0))
        value = 1.0 / (1.0 + math.exp(-strength * (numeric - 0.5) * 10.0))
        span = max(high - low, 1e-12)
        return max(0.001, min((value - low) / span, 0.999))

    @staticmethod
    def _calibrate_setup_prior(score: float, *, history_count: int = MIN_HISTORY_SAMPLES) -> float:
        if history_count < MIN_HISTORY_SAMPLES:
            return 0.5  # ratio 0..1: neutral prior until 20 setup outcomes exist.
        numeric = 0.5 + (max(0.0, min(float(score), 1.0)) - 0.5) * 1.15
        return ConfluenceEngine._soft_clip_score(numeric, strength=0.72)

    @staticmethod
    def _calibrate_component_model(score: float) -> float:
        numeric = 0.5 + (max(0.0, min(float(score), 1.0)) - 0.5) * 1.35
        return ConfluenceEngine._soft_clip_score(numeric, strength=0.68)

    @staticmethod
    def _apply_component_edge(
        blended: float,
        *,
        signal: Signal,
        components: list[ComponentScore],
    ) -> float:
        usable = [item for item in components if item.available and item.weight > 0.0]
        if not usable:
            return max(0.0, min(blended, 1.0))
        strong = sum(1 for item in usable if item.raw >= 0.72)
        weak = sum(1 for item in usable if item.raw <= 0.30)
        directional_bonus = 0.0
        if signal.strategy_family in {"breakout", "continuation"}:
            directional_bonus += 0.015 * strong
        elif signal.strategy_family in {"reversal", "sentiment", "orderflow", "liquidity"}:
            directional_bonus += 0.012 * strong
        edge = directional_bonus - (0.025 * weak)
        if strong >= 3 and weak == 0:
            edge += 0.035
        if weak >= 3:
            edge -= 0.040
        return max(0.0, min(blended + edge + 0.025, 1.0))

    @staticmethod
    def _component_family_multiplier(name: str, signal: Signal) -> float:
        family = str(signal.strategy_family or "")
        profile = str(signal.confirmation_profile or "")
        if family in {"breakout", "continuation"} or profile == "trend_follow":
            return {
                "mtf_alignment": 1.20,
                "volume_quality": 1.15,
                "structure_clarity": 0.90,
                "risk_reward": 1.05,
                "funding_score": 0.65,
                "crowd_position": 0.90,
                "oi_momentum": 1.10,
            }.get(name, 1.0)
        if family in {"reversal", "sentiment", "liquidity", "orderflow"}:
            return {
                "mtf_alignment": 0.75,
                "volume_quality": 1.00,
                "structure_clarity": 1.10,
                "risk_reward": 1.00,
                "funding_score": 1.25,
                "crowd_position": 1.25,
                "oi_momentum": 1.05,
            }.get(name, 1.0)
        return 1.0

    @staticmethod
    def _has_finite_attr(prepared: PreparedSymbol, name: str) -> bool:
        value = getattr(prepared, name, None)
        if value is None:
            return False
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    @classmethod
    def _has_latest_feature(cls, prepared: PreparedSymbol, frame_name: str, column: str) -> bool:
        frame = getattr(prepared, frame_name, None)
        if frame is None or frame.is_empty() or column not in frame.columns:
            return False
        try:
            value = frame.item(-1, column)
        except (IndexError, TypeError, ValueError):
            return False
        try:
            return value is not None and math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    @classmethod
    def _has_structure_context(cls, prepared: PreparedSymbol) -> bool:
        if cls._has_finite_attr(prepared, "poc_1h"):
            return True
        frame = getattr(prepared, "work_1h", None)
        if frame is None or frame.is_empty():
            return False
        return all(column in frame.columns for column in ("high", "low", "ema20", "atr14"))

    @classmethod
    def _has_crowding_context(cls, prepared: PreparedSymbol) -> bool:
        flags = getattr(prepared, "data_freshness_flags", ()) or ()
        if "crowding_context_missing" in flags:
            return False
        return any(
            cls._has_finite_attr(prepared, name)
            for name in (
                "ls_ratio",
                "top_account_ls_ratio",
                "top_position_ls_ratio",
                "global_ls_ratio",
                "global_account_ls_ratio",
                "top_vs_global_ls_gap",
                "taker_ratio",
            )
        )

    @classmethod
    def _has_oi_or_flow_context(cls, prepared: PreparedSymbol, signal: Signal) -> bool:
        if cls._has_finite_attr(prepared, "oi_change_pct") or cls._has_finite_attr(
            prepared,
            "basis_pct",
        ):
            return True
        if getattr(signal, "orderflow_delta_ratio", None) is not None:
            return True
        return cls._has_latest_feature(prepared, "work_15m", "delta_ratio")

    @staticmethod
    def _microstructure_context(prepared: PreparedSymbol, signal: Signal) -> Any:
        row = {
            "symbol": signal.symbol,
            "direction": signal.direction,
            "price_change_pct": prepared.universe.price_change_pct,
            "funding_rate": prepared.funding_rate,
            "oi_change_pct": prepared.oi_change_pct,
            "global_account_ls_ratio": prepared.global_account_ls_ratio or prepared.global_ls_ratio,
            "top_account_ls_ratio": prepared.top_account_ls_ratio or prepared.ls_ratio,
            "top_position_ls_ratio": prepared.top_position_ls_ratio,
            "taker_ratio": prepared.taker_ratio,
            "bid_price": prepared.bid_price,
            "ask_price": prepared.ask_price,
            "depth_imbalance": prepared.depth_imbalance,
            "microprice_bias": prepared.microprice_bias,
            "basis_pct": prepared.basis_pct,
            "liquidation_score": prepared.liquidation_score,
        }
        return build_microstructure_context(row)

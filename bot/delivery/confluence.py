"""ConfluenceEngine - unified signal quality scoring."""

from __future__ import annotations

import logging
import math
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engine.errors import DEFENSIVE_EXC
from engine.features.microstructure import build_microstructure_context

from .scoring import (
    ScoringResult,
    _adx_strength,
    _aggression_shift_leg,
    _btc_correlation_penalty,
    _crowd_position,
    _depth_imbalance_leg,
    _funding_contrarian,
    _keltner_position,
    _liquidation_cluster_score,
    _macd_alignment,
    _mtf_alignment,
    _obv_alignment,
    _oi_momentum,
    _orderflow_imbalance_leg,
    _pivot_proximity,
    _regime_alignment_bonus,
    _risk_reward_quality,
    _session_killzone_score,
    _structure_clarity,
    _volume_profile_position,
    _volume_quality,
    _vwap_position,
)

if TYPE_CHECKING:
    from engine.domain.config import BotSettings
    from engine.domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.confluence")
MIN_HISTORY_SAMPLES = 10


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
    notes: dict[str, Any] = field(default_factory=dict)

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
            notes=dict(self.notes),
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
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
        if self.notes:
            payload["notes"] = dict(self.notes)
        return payload


class ConfluenceEngine:
    """Single entry point for signal quality assessment.

    Usage::

        engine = ConfluenceEngine(settings)
        result = engine.score(signal, prepared)
    """

    def __init__(self, settings: BotSettings, *, repository: Any | None = None) -> None:
        self.settings = settings
        self.repository = repository

    def score(self, signal: Signal, prepared: PreparedSymbol) -> ConfluenceResult:
        cfg = self.settings.scoring
        components = self._compute_components(signal, prepared, cfg)
        failed = [c.name for c in components if not c.available]
        if failed:
            LOG.info(
                "confluence_factors_failed | setup_id=%s symbol=%s passed=%s failed=%s",
                signal.setup_id,
                signal.symbol,
                [c.name for c in components if c.available and c.contribution > 0.0],
                failed,
            )
        model_score = sum(c.contribution for c in components)
        notes = self._scoring_notes(prepared, components)

        prior_w = max(0.0, min(cfg.setup_prior_weight, 1.0))
        history_count = self._resolve_history_count(signal)
        win_rate: float | None = None
        if history_count >= 5 and self.repository is not None:
            getter = getattr(self.repository, "setup_win_rate", None)
            if callable(getter):
                with suppress(DEFENSIVE_EXC):
                    win_rate = getter(signal.setup_id)
        calibrated_prior = self._calibrate_setup_prior(
            signal.score,
            history_count=history_count,
            win_rate=win_rate,
            min_history_samples=self._min_history_samples(),
        )
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
            notes=notes,
        )

    def _min_history_samples(self) -> int:
        delivery = getattr(self.settings, "delivery", None)
        return int(
            getattr(delivery, "min_sl_penalty_samples", MIN_HISTORY_SAMPLES) or MIN_HISTORY_SAMPLES
        )

    def _resolve_history_count(self, signal: Signal) -> int:
        min_samples = self._min_history_samples()
        history_count = int(
            getattr(
                signal,
                "setup_history_count",
                getattr(signal, "history_count", 0),
            )
            or 0
        )
        if history_count >= min_samples:
            return history_count
        tracking_ref = getattr(signal, "tracking_ref", None)
        if not tracking_ref:
            return history_count
        repo = self.repository
        if repo is None:
            return history_count
        getter = getattr(repo, "setup_history_count", None)
        if not callable(getter):
            return history_count
        try:
            loaded = int(getter(signal.setup_id))
        except (TypeError, ValueError):
            LOG.debug("setup_history_count lookup failed | setup_id=%s", signal.setup_id)
            return history_count
        except DEFENSIVE_EXC:
            LOG.debug(
                "setup_history_count lookup error | setup_id=%s",
                signal.setup_id,
                exc_info=True,
            )
            return history_count
        return max(history_count, loaded)

    @staticmethod
    def _scoring_notes(
        prepared: PreparedSymbol,
        components: list[ComponentScore],
    ) -> dict[str, Any]:
        flags = getattr(prepared, "data_freshness_flags", ()) or ()
        if "crowding_context_missing" not in flags:
            return {}
        crowd = next((item for item in components if item.name == "crowd_position"), None)
        if crowd is None or crowd.available:
            return {}
        redistributed = [
            item.name
            for item in components
            if item.available and item.weight > 0.0 and item.name != "crowd_position"
        ]
        return {
            "weight_redistribution": {
                "reason": "crowding_context_missing",
                "excluded_components": ["crowd_position"],
                "redistributed_to": redistributed,
            }
        }

    def _compute_components(
        self,
        signal: Signal,
        prepared: PreparedSymbol,
        cfg: Any,
    ) -> list[ComponentScore]:
        """16-component weighted scoring model.

        Three-layer architecture:

        1. HARD GATE (delivery_orchestrator:weighted_delivery_gate)
        3-of-5 model: trend, momentum, volume + HTF, microstructure

        2. WEIGHTED (this method, normalised to 1.0)
        Primary: mtf_alignment, volume_quality, structure_clarity, risk_reward,
        crowd_position, funding_score, oi_momentum
        Market context: microstructure_context, liquidation_cluster, session_killzone
        Indicator: macd_alignment, obv_alignment, adx_strength
        Structure: keltner_position, vwap_position, regime_alignment

        3. PRIOR BLENDING (score method)
        strategy.score (65% prior_weight) + weighted model (35%)
        → calibrated → component edge → final_score
        """
        funding_weight = max(0.0, float(cfg.weight_crowd_position) * 0.5)
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
                "raw": _volume_quality(prepared, signal),
                "available": self._has_latest_feature(
                    prepared,
                    f"work_{getattr(signal, 'entry_tf', '15m').split('+')[0].split('/')[0]}",
                    "volume_ratio20",
                )
                or self._has_latest_feature(prepared, "work_15m", "volume_ratio20"),
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
            {
                "name": "liquidation_cluster",
                "weight": float(getattr(cfg, "weight_liquidation_proximity", 0.04)),
                "raw": _liquidation_cluster_score(prepared, signal),
                "available": getattr(prepared, "liquidation_cascade_5m", None) is not None,
            },
            {
                "name": "session_killzone",
                "weight": float(getattr(cfg, "weight_session_killzone", 0.03)),
                "raw": _session_killzone_score(signal),
                "available": True,
            },
            {
                "name": "orderflow_imbalance",
                "weight": float(getattr(cfg, "weight_orderflow_imbalance", 0.04)),
                "raw": _orderflow_imbalance_leg(prepared, signal),
                "available": self._has_latest_feature(prepared, "work_15m", "delta_ratio"),
            },
            {
                "name": "aggression_shift",
                "weight": float(getattr(cfg, "weight_aggression_shift", 0.03)),
                "raw": _aggression_shift_leg(prepared, signal),
                "available": (
                    getattr(prepared, "aggression_shift", None) is not None
                    or self._has_latest_feature(prepared, "work_15m", "delta_ratio")
                ),
            },
            {
                "name": "depth_imbalance",
                "weight": float(getattr(cfg, "weight_depth_imbalance", 0.04)),
                "raw": _depth_imbalance_leg(prepared, signal),
                "available": getattr(prepared, "depth_imbalance", None) is not None,
            },
            {
                "name": "macd_alignment",
                "weight": float(getattr(cfg, "weight_macd_alignment", 0.05)),
                "raw": _macd_alignment(prepared, signal),
                "available": self._has_latest_feature(prepared, "work_15m", "macd_hist"),
            },
            {
                "name": "obv_alignment",
                "weight": float(getattr(cfg, "weight_obv_alignment", 0.03)),
                "raw": _obv_alignment(prepared, signal),
                "available": self._has_latest_feature(prepared, "work_15m", "obv_above_ema"),
            },
            {
                "name": "adx_strength",
                "weight": float(getattr(cfg, "weight_adx_strength", 0.04)),
                "raw": _adx_strength(prepared, signal),
                "available": self._has_latest_feature(prepared, "work_15m", "adx14"),
            },
            {
                "name": "keltner_position",
                "weight": float(getattr(cfg, "weight_keltner_position", 0.03)),
                "raw": _keltner_position(prepared, signal),
                "available": self._has_latest_feature(prepared, "work_15m", "kc_upper"),
            },
            {
                "name": "vwap_position",
                "weight": float(getattr(cfg, "weight_vwap_position", 0.03)),
                "raw": _vwap_position(prepared, signal),
                "available": self._has_latest_feature(prepared, "work_15m", "vwap_deviation_pct"),
            },
            {
                "name": "regime_alignment",
                "weight": float(getattr(cfg, "weight_regime_alignment", 0.04)),
                "raw": _regime_alignment_bonus(prepared, signal),
                "available": True,
            },
            {
                "name": "volume_profile",
                "weight": float(getattr(cfg, "weight_volume_profile", 0.03)),
                "raw": _volume_profile_position(prepared, signal),
                "available": (
                    getattr(prepared, "vah_15m", None) is not None
                    and getattr(prepared, "val_15m", None) is not None
                ),
            },
            {
                "name": "pivot_proximity",
                "weight": float(getattr(cfg, "weight_pivot_proximity", 0.03)),
                "raw": _pivot_proximity(prepared, signal),
                "available": self._has_structure_context(prepared),
            },
            {
                "name": "btc_correlation",
                "weight": float(getattr(cfg, "weight_btc_correlation", 0.04)),
                "raw": _btc_correlation_penalty(prepared, signal),
                "available": (
                    getattr(prepared, "btc_change_pct", None) is not None
                    or getattr(prepared, "eth_change_pct", None) is not None
                ),
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
        if weight_total <= 0.0:
            LOG.warning(
                "ConfluenceEngine no available weighted components | setup_id=%s",
                signal.setup_id,
            )
            return [
                self._component_from_spec({**spec, "weight": 0.0, "available": False})
                for spec in specs
            ]
        if __debug__:
            active_weight_sum = sum(
                max(0.0, float(spec["weight"]))
                for spec in specs
                if bool(spec["available"]) and float(spec["weight"]) > 0.0
            )
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
    def _calibrate_setup_prior(
        score: float,
        *,
        history_count: int = MIN_HISTORY_SAMPLES,
        win_rate: float | None = None,
        min_history_samples: int = MIN_HISTORY_SAMPLES,
    ) -> float:
        # Gradient trust: linearly ramp from neutral (0.5) to fully calibrated score
        # over 0..min_history_samples outcomes instead of hard binary threshold.
        history_weight = min(max(history_count, 0) / max(min_history_samples, 1), 1.0)
        calibrated = 0.5 + (max(0.0, min(float(score), 1.0)) - 0.5) * 1.15
        numeric = 0.5 + (calibrated - 0.5) * history_weight
        if win_rate is not None and history_count >= 5:
            # Blend signal score with actual win-rate; weight grows with more data
            wr_weight = min(history_count / 20.0, 1.0) * 0.35
            numeric = numeric * (1.0 - wr_weight) + max(0.0, min(float(win_rate), 1.0)) * wr_weight
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
                "macd_alignment": 1.10,
                "obv_alignment": 1.05,
                "adx_strength": 1.15,
                "keltner_position": 1.00,
                "vwap_position": 1.00,
                "regime_alignment": 1.20,
                "volume_profile": 1.05,
                "pivot_proximity": 1.00,
                "btc_correlation": 1.10,
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
                "macd_alignment": 0.85,
                "obv_alignment": 0.90,
                "adx_strength": 0.95,
                "keltner_position": 0.90,
                "vwap_position": 0.85,
                "regime_alignment": 0.80,
                "volume_profile": 0.85,
                "pivot_proximity": 1.10,
                "btc_correlation": 0.70,
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


WEIGHTED_HARD_LEG_KEYS = ("trend", "momentum", "volume")


def evaluate_weighted_delivery_gate(
    *,
    conf_result: ConfluenceResult,
    confirmations: dict[str, bool],
    action_min_score: float,
    min_hard_legs: int = 2,
    hard_leg_keys: tuple[str, ...] = WEIGHTED_HARD_LEG_KEYS,
) -> tuple[bool, dict[str, object]]:
    """Weighted ConfluenceEngine score is primary; boolean legs are a hard floor."""
    weighted_min = float(action_min_score)
    weighted_pass = conf_result.final_score >= weighted_min
    hard_legs = sum(1 for key in hard_leg_keys if confirmations.get(key))
    required_hard = max(1, min(int(min_hard_legs), len(hard_leg_keys)))
    passed = weighted_pass and hard_legs >= required_hard
    details: dict[str, object] = {
        "weighted_confluence_primary": True,
        "confluence_engine": conf_result.to_dict(),
        "weighted_confluence_pass": weighted_pass,
        "weighted_hard_legs": hard_legs,
        "weighted_hard_legs_required": required_hard,
        "weighted_confluence_primary_pass": passed,
        "weighted_min_score": weighted_min,
    }
    if not passed:
        if not weighted_pass:
            details["reason"] = "weighted_confluence_below_min"
        else:
            details["reason"] = "weighted_hard_legs_insufficient"
    return passed, details

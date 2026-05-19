"""ConfluenceEngine — unified signal quality scoring."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .domain.config import BotSettings
from .domain.schemas import PreparedSymbol, Signal
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

LOG = logging.getLogger("bot.confluence")


@dataclass(frozen=True, slots=True)
class ComponentScore:
    """Score contribution from a single factor."""

    name: str
    raw: float
    weight: float
    contribution: float


@dataclass(frozen=True)
class ConfluenceResult:
    """Full quality assessment of a signal."""

    setup_prior: float
    components: tuple[ComponentScore, ...]
    final_score: float

    @property
    def weighted_model_score(self) -> float:
        return sum(c.contribution for c in self.components)

    def to_scoring_result(self) -> ScoringResult:
        adjustments = {c.name: c.contribution for c in self.components}
        return ScoringResult(
            base_score=self.setup_prior,
            adjustments=adjustments,
            final_score=self.final_score,
            setup_id="",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "setup_prior": self.setup_prior,
            "components": [
                {
                    "name": c.name,
                    "raw": c.raw,
                    "weight": c.weight,
                    "contribution": c.contribution,
                }
                for c in self.components
            ],
            "weighted_model_score": self.weighted_model_score,
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
        blended = (signal.score * prior_w) + (model_score * (1.0 - prior_w))
        final = round(max(0.0, min(blended, 1.0)), 4)

        return ConfluenceResult(
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
        specs = [
            (
                "mtf_alignment",
                cfg.weight_mtf_alignment,
                _mtf_alignment(prepared, signal),
            ),
            ("volume_quality", cfg.weight_volume_quality, _volume_quality(prepared)),
            (
                "structure_clarity",
                cfg.weight_structure_clarity,
                _structure_clarity(prepared, signal),
            ),
            (
                "risk_reward",
                cfg.weight_risk_reward,
                _risk_reward_quality(signal, self.settings),
            ),
            (
                "funding_score",
                funding_weight,
                _funding_contrarian(prepared, signal, self.settings),
            ),
            (
                "crowd_position",
                crowd_weight,
                _crowd_position(prepared, signal, self.settings),
            ),
            ("oi_momentum", cfg.weight_oi_momentum, _oi_momentum(prepared, signal)),
        ]
        weight_total = sum(max(0.0, float(weight)) for _, weight, _ in specs)
        if weight_total > 0.0:
            specs = [
                (name, max(0.0, float(weight)) / weight_total, raw) for name, weight, raw in specs
            ]
        return [
            ComponentScore(
                name=name,
                raw=round(raw, 4),
                weight=weight,
                contribution=round(weight * raw, 4),
            )
            for name, weight, raw in specs
        ]

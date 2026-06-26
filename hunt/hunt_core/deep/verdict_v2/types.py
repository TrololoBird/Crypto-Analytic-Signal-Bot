"""Verdict V2 datatypes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PathType = Literal[
    "continuation_up",
    "continuation_down",
    "local_impulse_up",
    "local_impulse_down",
    "pullback_up",
    "pullback_down",
    "range",
    "breakout_up",
    "breakout_down",
    "squeeze_up",
    "squeeze_down",
]
PathDirection = Literal["long", "short", "neutral", "weak_long", "weak_short"]
SignalAction = Literal["long", "short", "wait"]
EntryType = Literal["market", "pullback_limit", "breakout"]
CatalystKind = Literal[
    "structure_break",
    "poc_loss",
    "poc_reclaim",
    "funding_flush",
    "liq_sweep",
    "level_break",
    "flow_confirmation",
]
DriverKind = Literal[
    "trend_driven",
    "liquidity_driven",
    "positioning_driven",
    "flow_driven",
    "unknown",
]
TopologyKind = Literal[
    "aligned_trend",
    "bull_pullback",
    "bear_rally",
    "compression",
    "reversal_candidate",
    "mixed",
]
DisagreementKind = Literal[
    "consensus",
    "divergence",
    "transition",
    "exhaustion",
    "compression",
    "expansion",
]
HorizonKey = Literal["A", "B", "C"]


@dataclass(frozen=True, slots=True)
class EngineOutput:
    long: float
    short: float
    conviction: float
    blend_weight: float
    coverage_quality: float
    information_value: float
    evidence: list[str] = field(default_factory=list)
    factors_used: int = 0
    factors_available: int = 0
    upside_reward_pct: float = 0.0
    downside_reward_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class HorizonForecast:
    key: HorizonKey
    long: float
    short: float
    dominant: PathDirection
    conviction: float
    range_probability: float = 0.0


@dataclass(frozen=True, slots=True)
class HorizonTopology:
    kind: TopologyKind
    a_dominant: str
    b_dominant: str
    c_dominant: str
    coherence: float
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DisagreementState:
    state: DisagreementKind
    score: float
    conflict_matrix: dict[str, float]
    dominant_conflict: str | None
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MarketDriver:
    primary: DriverKind
    secondary: str | None
    hypothesis: str
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PatternCandidate:
    id: str
    raw_score: float
    direction_hint: PathDirection
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PatternConfidence:
    primary: PatternCandidate
    alternatives: tuple[PatternCandidate, ...]
    spread: float
    ambiguous: bool


@dataclass(frozen=True, slots=True)
class ExpectedPath:
    type: PathType
    direction: PathDirection
    expected_move_pct: tuple[float, float]
    expected_time_h: tuple[float, float]
    invalidation: float
    probability_rank: float
    narrative: str
    supporting_patterns: list[str] = field(default_factory=list)
    topology: str = ""


@dataclass(frozen=True, slots=True)
class ScenarioCatalyst:
    primary: CatalystKind
    label: str
    trigger_level: float | None
    confidence: float
    alternatives: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ScenarioFragility:
    score: float
    label: Literal["low", "moderate", "high"]
    dependencies: list[str] = field(default_factory=list)
    break_conditions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SignalStrength:
    score: float
    label: Literal["strong", "moderate", "weak"]
    capped_by_data: bool
    disclaimer: str = "rank only — not win probability"
    breakdown: dict[str, float] = field(default_factory=dict)
    scenario_confidence: float = 0.0
    geometry_confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class TradeQuality:
    score: float
    rr_nearest: float
    rr_stretch: float
    verdict: Literal["favorable", "marginal", "poor"]
    advisory: str = ""


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    coverage_score: float
    missing_groups: list[str] = field(default_factory=list)
    sources: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MaturityFeatures:
    maturity_score: float
    trend_age: float
    bars_since_cross: float
    ema_separation_pct: float
    evidence: list[str] = field(default_factory=list)


PlanLifecycle = Literal["forming", "armed", "active"]


@dataclass(frozen=True, slots=True)
class TradePlan:
    direction: Literal["long", "short"]
    entry_type: EntryType
    entry_zone: tuple[float, float]
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    rr_tp1: float
    rr_tp2: float
    rr_tp3: float
    rr_primary: float
    invalidation_reason: str
    level_sources: list[str] = field(default_factory=list)
    entry_reference: float = 0.0
    rr_conservative_tp1: float = 0.0
    rr_conservative_tp2: float = 0.0
    rr_conservative_tp3: float = 0.0
    rr_base_label: str = "≈R:R (от края зоны)"
    plan_lifecycle: PlanLifecycle = "forming"


@dataclass(frozen=True, slots=True)
class SignalDecision:
    action: SignalAction
    reason: str
    gates_failed: list[str] = field(default_factory=list)
    trade_plan: TradePlan | None = None
    wait_category: str = ""


@dataclass(frozen=True, slots=True)
class ScenarioVerdict:
    signal_decision: SignalDecision
    trade_plan: TradePlan | None
    expected_path: ExpectedPath
    catalyst: ScenarioCatalyst
    signal_strength: SignalStrength
    fragility: ScenarioFragility
    trade_quality: TradeQuality
    pattern_confidence: PatternConfidence
    horizon_topology: HorizonTopology
    market_driver: MarketDriver
    disagreement: DisagreementState
    engine_outputs: dict[str, EngineOutput]
    conflict_matrix: dict[str, float]
    horizons: dict[str, HorizonForecast]
    data_quality: DataQualityReport
    maturity: MaturityFeatures
    market_context: str
    evidence: list[str] = field(default_factory=list)
    reconcile_level: str = "coherent"
    reconcile_caveats: tuple[str, ...] = ()
    factor_contributions: tuple[Any, ...] = ()

    def to_audit_dict(self) -> dict[str, Any]:
        pc = self.pattern_confidence
        dec = self.signal_decision
        plan = self.trade_plan
        path = self.expected_path
        out: dict[str, Any] = {
            "path": path.type,
            "path_direction": path.direction,
            "action": dec.action,
            "reason": dec.reason,
            "gates_failed": list(dec.gates_failed),
            "patterns": [pc.primary.id, *[a.id for a in pc.alternatives]],
            "secondary_paths": [a.id for a in pc.alternatives[:2]],
            "pattern_spread": pc.spread,
            "topology": self.horizon_topology.kind,
            "driver": self.market_driver.primary,
            "strength": self.signal_strength.score,
            "strength_label": self.signal_strength.label,
            "scenario_confidence": self.signal_strength.scenario_confidence,
            "geometry_confidence": self.signal_strength.geometry_confidence,
            "data_completeness": sum(self.data_quality.sources.values()) / max(len(self.data_quality.sources), 1) if self.data_quality.sources else self.data_quality.coverage_score,
            "wait_category": self.signal_decision.wait_category,
            "fragility": self.fragility.score,
            "fragility_label": self.fragility.label,
            "trade_quality": self.trade_quality.verdict,
            "rr_primary": plan.rr_primary if plan else 0.0,
            "catalyst": self.catalyst.primary,
            "catalyst_level": self.catalyst.trigger_level,
            "market_context": self.market_context,
            "data_coverage": self.data_quality.coverage_score,
        }
        if plan:
            out["entry_lo"] = plan.entry_zone[0]
            out["entry_hi"] = plan.entry_zone[1]
            out["stop"] = plan.stop_loss
            out["tp1"] = plan.take_profit_1
            out["tp2"] = plan.take_profit_2
            out["tp3"] = plan.take_profit_3
            out["rr_base_label"] = plan.rr_base_label
            out["plan_lifecycle"] = plan.plan_lifecycle
            out["entry_reference"] = plan.entry_reference
            out["entry_type"] = plan.entry_type
            from hunt_core.deep.plan import plan_geometry_valid

            out["geometry_valid"] = plan_geometry_valid(
                {"entry_zone": list(plan.entry_zone), "tp1": plan.take_profit_1},
                direction=plan.direction,  # type: ignore[arg-type]
            )
        out["reconcile_level"] = self.reconcile_level
        if self.reconcile_caveats:
            out["reconcile_caveats"] = list(self.reconcile_caveats)
        h_b = self.horizons.get("B")
        if h_b:
            out["horizon_b_conviction"] = h_b.conviction
            out["range_probability"] = h_b.range_probability
        return out

    def to_summary_dict(self) -> dict[str, Any]:
        """JSONL-safe summary — no nested dataclasses."""
        return self.to_audit_dict()

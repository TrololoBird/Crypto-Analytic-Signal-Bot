"""Expansion Engine datatypes.

The engine answers "why might an impulse start *now*?" — it returns an
:class:`ExpansionOpportunity`, never a long/short verdict. State is derived *after*
the probability model, not before.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ExpansionStateKind = Literal[
    "neutral",
    "accumulation",
    "distribution",
    "pre_pump",
    "pre_dump",
    "active_pump",
    "active_dump",
]
Direction = Literal["up", "down", "neutral"]
Readiness = Literal["low", "medium", "high"]
Risk = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class BlockResult:
    """One block's reading: unsigned magnitude + the side it argues for.

    ``score`` is a 0..1 magnitude. ``direction`` says which expansion side the block
    supports (``up`` ⇒ pre-pump evidence, ``down`` ⇒ pre-dump). ``active`` is False
    when inputs were missing — the block then contributes nothing and is excluded from
    coverage.
    """

    name: str
    score: float
    direction: Direction = "neutral"
    active: bool = True
    evidence: tuple[str, ...] = ()

    def signed(self) -> float:
        if not self.active or self.direction == "neutral":
            return 0.0
        return self.score if self.direction == "up" else -self.score


@dataclass(frozen=True, slots=True)
class BlockScores:
    # Tier A — Core
    compression: float = 0.0
    absorption: float = 0.0
    fuel: float = 0.0
    funding: float = 0.0
    liquidity: float = 0.0
    structure: float = 0.0
    strength: float = 0.0
    fuel_imbalance: float = 0.0
    supply_exhaustion: float = 0.0
    trigger_proximity: float = 0.0
    # Tier B — Smart Money
    market_maker_trap: float = 0.0
    liquidity_sweep: float = 0.0
    distribution_quality: float = 0.0
    fractal_alignment: float = 0.0
    cycle_context: float = 0.0
    state_persistence: float = 0.0
    # Tier C — Expansion Intelligence
    liquidity_vacuum: float = 0.0
    short_squeeze_potential: float = 0.0
    long_squeeze_potential: float = 0.0
    oi_concentration: float = 0.0
    breakout_failure: float = 0.0
    wyckoff_spring: float = 0.0
    wyckoff_upthrust: float = 0.0
    wyckoff_sos: float = 0.0
    wyckoff_sow: float = 0.0
    volatility_regime: float = 0.0
    whale_activity: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BlockDeltas:
    """Trajectory of selected blocks over the configured lookback (slope, -1..1)."""

    compression: float = 0.0
    oi: float = 0.0
    funding: float = 0.0
    liquidity: float = 0.0
    structure: float = 0.0
    fuel_imbalance: float = 0.0
    supply_exhaustion: float = 0.0
    momentum: float = 0.0  # aggregate acceleration across tracked blocks

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExpansionProbabilities:
    p_up: float
    p_down: float
    p_none: float

    def to_dict(self) -> dict[str, float]:
        return {"p_up": self.p_up, "p_down": self.p_down, "p_none": self.p_none}


@dataclass(frozen=True, slots=True)
class MetaScores:
    expansion_quality: float
    fake_breakout_risk: float
    opportunity_score: float
    sector_rotation: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExpansionForecast:
    expected_move_pct: tuple[float, float]
    expected_horizon_h: tuple[float, float]
    main_drivers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_move_pct": list(self.expected_move_pct),
            "expected_horizon_h": list(self.expected_horizon_h),
            "main_drivers": list(self.main_drivers),
        }


@dataclass(frozen=True, slots=True)
class ExpansionExecution:
    entry_band: tuple[float, float]
    activation: float
    stop: float
    targets: tuple[float, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_band": list(self.entry_band),
            "activation": self.activation,
            "stop": self.stop,
            "targets": list(self.targets),
        }


@dataclass(frozen=True, slots=True)
class ExpansionOpportunity:
    symbol: str
    price: float
    # Level 1 — state
    state: ExpansionStateKind
    stage: str
    lifecycle_stage: int
    probabilities: ExpansionProbabilities
    expansion_score: float
    trigger_probability: float
    meta: MetaScores
    blocks: BlockScores
    deltas: BlockDeltas
    main_drivers: tuple[str, ...]
    readiness: Readiness
    risk: Risk
    coverage: float
    coverage_ceiling: str = "market_data_only"
    # Level 2 — forecast (None below threshold)
    forecast: ExpansionForecast | None = None
    # Level 3 — execution (None below threshold)
    execution: ExpansionExecution | None = None
    evidence: tuple[str, ...] = ()

    @property
    def dominant(self) -> Direction:
        p = self.probabilities
        if p.p_up >= p.p_down and p.p_up > p.p_none:
            return "up"
        if p.p_down > p.p_up and p.p_down > p.p_none:
            return "down"
        return "neutral"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "symbol": self.symbol,
            "price": self.price,
            "state": self.state,
            "stage": self.stage,
            "lifecycle_stage": self.lifecycle_stage,
            "probabilities": self.probabilities.to_dict(),
            "expansion_score": self.expansion_score,
            "trigger_probability": self.trigger_probability,
            "dominant": self.dominant,
            "meta": self.meta.to_dict(),
            "blocks": self.blocks.to_dict(),
            "deltas": self.deltas.to_dict(),
            "main_drivers": list(self.main_drivers),
            "readiness": self.readiness,
            "risk": self.risk,
            "coverage": self.coverage,
            "coverage_ceiling": self.coverage_ceiling,
            "evidence": list(self.evidence),
            "forecast": self.forecast.to_dict() if self.forecast else None,
            "execution": self.execution.to_dict() if self.execution else None,
        }
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExpansionOpportunity:
        """Rebuild from ``to_dict()`` / stamped ``row["expansion"]`` (scan fast path)."""
        if not isinstance(d, dict):
            raise TypeError("expansion dict required")

        def _dc(raw: dict[str, Any], dc_cls: type) -> Any:
            fields = dc_cls.__dataclass_fields__
            kwargs: dict[str, Any] = {}
            for name in fields:
                if name in raw:
                    kwargs[name] = raw[name]
            return dc_cls(**kwargs)

        meta_raw = d.get("meta") if isinstance(d.get("meta"), dict) else {}
        probs_raw = d.get("probabilities") if isinstance(d.get("probabilities"), dict) else {}
        blocks_raw = d.get("blocks") if isinstance(d.get("blocks"), dict) else {}
        deltas_raw = d.get("deltas") if isinstance(d.get("deltas"), dict) else {}

        forecast: ExpansionForecast | None = None
        fc = d.get("forecast") if isinstance(d.get("forecast"), dict) else None
        if fc and fc.get("expected_move_pct"):
            mv = fc["expected_move_pct"]
            hz = fc.get("expected_horizon_h") or (0.0, 0.0)
            forecast = ExpansionForecast(
                expected_move_pct=(float(mv[0]), float(mv[1])),
                expected_horizon_h=(float(hz[0]), float(hz[1])),
                main_drivers=tuple(fc.get("main_drivers") or ()),
            )

        execution: ExpansionExecution | None = None
        ex = d.get("execution") if isinstance(d.get("execution"), dict) else None
        if ex and ex.get("entry_band"):
            eb = ex["entry_band"]
            execution = ExpansionExecution(
                entry_band=(float(eb[0]), float(eb[1])),
                activation=float(ex.get("activation") or 0.0),
                stop=float(ex.get("stop") or 0.0),
                targets=tuple(float(t) for t in (ex.get("targets") or ())),
            )

        drivers = d.get("main_drivers") or []
        evidence = d.get("evidence") or []
        return cls(
            symbol=str(d.get("symbol") or ""),
            price=float(d.get("price") or 0.0),
            state=str(d.get("state") or "neutral"),  # type: ignore[arg-type]
            stage=str(d.get("stage") or ""),
            lifecycle_stage=int(d.get("lifecycle_stage") or 0),
            probabilities=_dc(probs_raw, ExpansionProbabilities),
            expansion_score=float(d.get("expansion_score") or 0.0),
            trigger_probability=float(d.get("trigger_probability") or 0.0),
            meta=_dc(meta_raw, MetaScores),
            blocks=_dc(blocks_raw, BlockScores),
            deltas=_dc(deltas_raw, BlockDeltas),
            main_drivers=tuple(str(x) for x in drivers),
            readiness=str(d.get("readiness") or "low"),  # type: ignore[arg-type]
            risk=str(d.get("risk") or "medium"),  # type: ignore[arg-type]
            coverage=float(d.get("coverage") or 0.0),
            coverage_ceiling=str(d.get("coverage_ceiling") or "market_data_only"),
            forecast=forecast,
            execution=execution,
            evidence=tuple(str(x) for x in evidence),
        )


@dataclass(frozen=True, slots=True)
class BlockContext:
    """Pre-parsed views of the tick row, passed to every block scorer."""

    row: dict[str, Any]
    symbol: str
    price: float
    market: dict[str, Any]
    maps: dict[str, Any]
    structure: dict[str, Any]
    regime: dict[str, Any]
    timeframes: dict[str, Any]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> BlockContext:
        from hunt_core.expansion._util import (
            market_of,
            maps_of,
            regime_of,
            safe_float,
            structure_of,
            timeframes_of,
        )

        return cls(
            row=row,
            symbol=str(row.get("symbol") or "").upper(),
            price=safe_float(row.get("price")),
            market=market_of(row),
            maps=maps_of(row),
            structure=structure_of(row),
            regime=regime_of(row),
            timeframes=timeframes_of(row),
        )

    def tf(self, key: str) -> dict[str, Any]:
        from hunt_core.expansion._util import tf_snap

        return tf_snap(self.row, key)


@dataclass
class BlockBundle:
    """All block readings for one tick — magnitudes plus direction/evidence."""

    results: dict[str, BlockResult] = field(default_factory=dict)

    def scores(self) -> BlockScores:
        vals = {name: res.score if res.active else 0.0 for name, res in self.results.items()}
        valid = {k: v for k, v in vals.items() if k in BlockScores.__dataclass_fields__}
        return BlockScores(**valid)

    def active_count(self) -> int:
        return sum(1 for r in self.results.values() if r.active)

    def coverage(self) -> float:
        total = len(self.results)
        if total <= 0:
            return 0.0
        return self.active_count() / total

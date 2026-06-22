"""Pattern generators and top-3 resolver."""
from __future__ import annotations

from typing import Any

from hunt_core.deep.verdict_v2._helpers import clamp01, safe_float
from hunt_core.deep.verdict_v2.config import VerdictV2Config
from hunt_core.deep.verdict_v2.market_driver import filter_patterns_by_driver
from hunt_core.deep.verdict_v2.types import (
    HorizonTopology,
    MarketDriver,
    MaturityFeatures,
    PatternCandidate,
    PatternConfidence,
)


def _gen_trend(row: dict[str, Any], topo: HorizonTopology, mat: MaturityFeatures) -> PatternCandidate:
    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    bias = str(structure.get("structure_bias") or "wait")
    score = 0.45
    direction: str = "neutral"
    evidence: list[str] = []
    if topo.kind == "aligned_trend":
        score += 0.25
        direction = topo.a_dominant
        evidence.append("aligned_trend")
    elif topo.kind == "bull_pullback":
        score += 0.2
        direction = "long"
        evidence.append("bull_pullback")
    elif topo.kind == "bear_rally":
        score += 0.2
        direction = "short"
        evidence.append("bear_rally")
    if bias == "long":
        score += 0.1
        direction = "long"
    elif bias == "short":
        score += 0.1
        direction = "short"
    if mat.maturity_score > 0.5:
        score += 0.05
        evidence.append("mature_trend")
    pid = "trend_continuation" if direction != "short" else "distribution"
    return PatternCandidate(id=pid, raw_score=clamp01(score), direction_hint=direction, evidence=evidence)  # type: ignore[arg-type]


def _gen_mean_reversion(row: dict[str, Any], topo: HorizonTopology) -> PatternCandidate:
    regime = row.get("regime") if isinstance(row.get("regime"), dict) else {}
    price = safe_float(row.get("price"))
    poc = safe_float(regime.get("poc_1h"))
    score = 0.35
    direction: str = "neutral"
    evidence: list[str] = []
    if topo.kind == "compression":
        score += 0.25
        evidence.append("compression")
    if poc > 0 and price > 0:
        dist = abs(price - poc) / price * 100
        if dist > 1.5:
            score += 0.15
            direction = "long" if price < poc else "short"
            evidence.append("far_from_poc")
    return PatternCandidate(id="range_bound", raw_score=clamp01(score), direction_hint=direction, evidence=evidence)  # type: ignore[arg-type]


def _gen_liquidity(row: dict[str, Any]) -> PatternCandidate:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    mps = safe_float(market.get("liq_magnet_pull_short_pct"))
    mpl = safe_float(market.get("liq_magnet_pull_long_pct"))
    score = 0.3
    direction: str = "neutral"
    evidence: list[str] = []
    if mps > mpl and mps > 0.5:
        score += min(0.35, mps / 10)
        direction = "short"
        evidence.append("liq_magnet_down")
        pid = "long_squeeze"
    elif mpl > 0.5:
        score += min(0.35, mpl / 10)
        direction = "long"
        evidence.append("liq_magnet_up")
        pid = "short_squeeze"
    else:
        pid = "liquidity_sweep"
    return PatternCandidate(id=pid, raw_score=clamp01(score), direction_hint=direction, evidence=evidence)  # type: ignore[arg-type]


def _gen_distribution(row: dict[str, Any], ctx: str) -> PatternCandidate:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    fz = safe_float(market.get("funding_zscore_48h"))
    score = 0.32
    direction: str = "short"
    evidence: list[str] = []
    if ctx == "bull_distribution":
        score += 0.22
        evidence.append("bull_distribution_ctx")
    if fz > 1.0:
        score += 0.15
        evidence.append("crowded_funding")
    return PatternCandidate(id="distribution", raw_score=clamp01(score), direction_hint=direction, evidence=evidence)  # type: ignore[arg-type]


def generate_patterns(
    row: dict[str, Any],
    topo: HorizonTopology,
    mat: MaturityFeatures,
    ctx: str,
    driver: MarketDriver,
    cfg: VerdictV2Config,
) -> PatternConfidence:
    raw = [
        _gen_trend(row, topo, mat),
        _gen_mean_reversion(row, topo),
        _gen_liquidity(row),
        _gen_distribution(row, ctx),
    ]
    filtered = filter_patterns_by_driver(raw, driver)
    ranked = sorted(filtered, key=lambda c: c.raw_score, reverse=True)
    primary = ranked[0]
    alts = tuple(ranked[1:3])
    spread = primary.raw_score - (alts[0].raw_score if alts else 0)
    ambiguous = spread < cfg.pattern_ambiguity_spread
    resolved = PatternConfidence(
        primary=primary,
        alternatives=alts,
        spread=round(spread, 3),
        ambiguous=ambiguous,
    )
    if row.get("symbol"):
        from hunt_core.deep.verdict_v2.audit import append_pattern_audit

        append_pattern_audit(row, raw=raw, filtered=filtered, resolved=resolved, driver=driver)
    return resolved

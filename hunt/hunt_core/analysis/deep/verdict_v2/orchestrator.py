"""Verdict V2 orchestrator — L0–L5 pipeline."""
from __future__ import annotations

from typing import Any

from hunt_core.analysis.deep.verdict_v2.blender import blend_horizons, build_conflict_matrix
from hunt_core.analysis.deep.verdict_v2.catalyst import build_catalyst
from hunt_core.analysis.deep.verdict_v2.config import VerdictV2Config, load_verdict_v2_config
from hunt_core.analysis.deep.verdict_v2.context import classify_market_context
from hunt_core.analysis.deep.verdict_v2.disagreement import classify_disagreement
from hunt_core.analysis.deep.verdict_v2.engines import run_all_engines, run_data_quality
from hunt_core.analysis.deep.verdict_v2.features.maturity import extract_maturity
from hunt_core.analysis.deep.verdict_v2.fragility import compute_fragility
from hunt_core.analysis.deep.verdict_v2.horizon_topology import classify_topology
from hunt_core.analysis.deep.verdict_v2.market_driver import infer_driver
from hunt_core.analysis.deep.verdict_v2.path_mapper import map_to_expected_path
from hunt_core.analysis.deep.verdict_v2.patterns import generate_patterns
from hunt_core.analysis.deep.verdict_v2.signal_decision import decide_signal
from hunt_core.analysis.deep.verdict_v2.signal_strength import compute_signal_strength
from hunt_core.analysis.deep.verdict_v2.timing_gate import assess_timing_gate
from hunt_core.analysis.deep.verdict_v2.trade_plan import build_trade_plan
from hunt_core.analysis.deep.verdict_v2.trade_quality import compute_trade_quality
from hunt_core.analysis.deep.verdict_v2.types import ScenarioVerdict


def build_scenario_verdict(
    row: dict[str, Any],
    *,
    cfg: VerdictV2Config | None = None,
) -> ScenarioVerdict:
    cfg = cfg or load_verdict_v2_config()
    sym = str(row.get("symbol") or "").upper()

    engines = run_all_engines(row)
    data_quality = run_data_quality(row)
    horizons = blend_horizons(engines, cfg)
    conflict_matrix = build_conflict_matrix(engines)
    topology = classify_topology(horizons)
    disagreement = classify_disagreement(horizons, conflict_matrix, cfg)
    market_context = classify_market_context(row)
    maturity = extract_maturity(row)

    engine_evidence: list[str] = []
    for eng in engines.values():
        engine_evidence.extend(eng.evidence[:2])

    driver = infer_driver(engine_evidence, "")
    patterns = generate_patterns(row, topology, maturity, market_context, driver, cfg)
    driver = infer_driver(engine_evidence, patterns.primary.id)
    path = map_to_expected_path(row, patterns, topology)
    catalyst = build_catalyst(row, path)
    fragility = compute_fragility(path, topology, disagreement, patterns, cfg)
    strength = compute_signal_strength(
        path,
        horizons,
        fragility,
        disagreement,
        data_quality,
        symbol=sym,
        topology_kind=topology.kind,
    )
    plan = build_trade_plan(row, path, cfg.trade_plan)
    trade_q = compute_trade_quality(plan, cfg)
    timing = assess_timing_gate(row, path.direction, horizons=horizons)
    decision = decide_signal(
        path, strength, fragility, trade_q, plan, data_quality, catalyst, cfg, timing=timing
    )

    evidence = [
        f"ctx={market_context}",
        f"topo={topology.kind}",
        f"path={path.type}",
        f"driver={driver.primary}",
    ]
    if timing.evidence:
        evidence.append(f"timing={timing.evidence[0]}")
    h_c = horizons.get("C")
    if h_c:
        evidence.append(f"range_p={h_c.range_probability:.2f}")
    return ScenarioVerdict(
        signal_decision=decision,
        trade_plan=plan,
        expected_path=path,
        catalyst=catalyst,
        signal_strength=strength,
        fragility=fragility,
        trade_quality=trade_q,
        pattern_confidence=patterns,
        horizon_topology=topology,
        market_driver=driver,
        disagreement=disagreement,
        engine_outputs=engines,
        conflict_matrix=conflict_matrix,
        horizons=horizons,
        data_quality=data_quality,
        maturity=maturity,
        market_context=market_context,
        evidence=evidence,
    )

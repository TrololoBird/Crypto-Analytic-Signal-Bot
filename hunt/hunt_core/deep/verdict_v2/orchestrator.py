"""Verdict V2 orchestrator — L0–L5 pipeline."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

from hunt_core.deep.verdict_v2.blender import blend_horizons, build_conflict_matrix
from hunt_core.deep.verdict_v2._helpers import atr_from_row
from hunt_core.deep.verdict_v2.catalyst import build_catalyst
from hunt_core.deep.verdict_v2.config import VerdictV2Config, load_verdict_v2_config
from hunt_core.deep.verdict_v2.context import classify_market_context
from hunt_core.deep.verdict_v2.disagreement import classify_disagreement
from hunt_core.deep.verdict_v2.engines import run_all_engines, run_data_quality
from hunt_core.deep.verdict_v2.maturity import extract_maturity
from hunt_core.deep.verdict_v2.fragility import compute_fragility
from hunt_core.deep.verdict_v2.horizon_topology import classify_topology
from hunt_core.deep.verdict_v2.market_driver import infer_driver
from hunt_core.deep.verdict_v2.path_mapper import adjust_expected_move_from_plan, map_to_expected_path
from hunt_core.deep.verdict_v2.patterns import generate_patterns
from hunt_core.deep.verdict_v2.reconcile import reconcile_context
from hunt_core.deep.verdict_v2.signal_decision import decide_signal
from hunt_core.deep.verdict_v2.signal_strength import apply_reconcile_to_strength, compute_signal_strength
from hunt_core.deep.verdict_v2.timing_gate import assess_timing_gate
from hunt_core.deep.verdict_v2.trade_plan import build_trade_plan
from hunt_core.deep.verdict_v2.trade_quality import apply_reconcile_to_trade_quality, compute_trade_quality
from hunt_core.deep.verdict_v2.types import ScenarioVerdict


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
    plan = build_trade_plan(row, path, cfg.trade_plan)
    catalyst = build_catalyst(
        row, path,
        entry_zone=plan.entry_zone if plan else None,
        atr=atr_from_row(row),
    )
    path = adjust_expected_move_from_plan(path, plan)
    fragility = compute_fragility(
        path, topology, disagreement, patterns, cfg, plan=plan, row=row
    )
    strength = compute_signal_strength(
        path,
        horizons,
        fragility,
        disagreement,
        data_quality,
        symbol=sym,
        topology_kind=topology.kind,
        engines=engines,
        plan=plan,
    )
    reconcile = reconcile_context(row, path, plan, engines, patterns)
    from hunt_core.deep.verdict_v2.path_shadow import (
        append_reconcile_path_shadow,
        compute_shadow_path,
        reconcile_flip_path_enabled,
    )

    shadow_path = compute_shadow_path(
        row,
        path,
        reconcile_conflicts=reconcile.conflicts,
        patterns=patterns,
        topology=topology,
    )
    path_for_decision = path
    if reconcile_flip_path_enabled() and shadow_path is not None:
        path_for_decision = shadow_path
        plan = build_trade_plan(row, path_for_decision, cfg.trade_plan)
        path_for_decision = adjust_expected_move_from_plan(path_for_decision, plan)
        fragility = compute_fragility(
            path_for_decision, topology, disagreement, patterns, cfg, plan=plan, row=row
        )
        strength = compute_signal_strength(
            path_for_decision,
            horizons,
            fragility,
            disagreement,
            data_quality,
            symbol=sym,
            topology_kind=topology.kind,
            engines=engines,
            plan=plan,
        )
        reconcile = reconcile_context(row, path_for_decision, plan, engines, patterns)
    strength = apply_reconcile_to_strength(strength, reconcile)
    trade_q = compute_trade_quality(plan, cfg)
    trade_q = apply_reconcile_to_trade_quality(trade_q, reconcile)
    timing = assess_timing_gate(row, path_for_decision.direction, horizons=horizons)
    decision = decide_signal(
        path_for_decision,
        strength,
        fragility,
        trade_q,
        plan,
        data_quality,
        catalyst,
        cfg,
        timing=timing,
        reconcile=reconcile,
        row=row,
    )

    evidence = [
        f"ctx={market_context}",
        f"topo={topology.kind}",
        f"path={path.type}",
        f"driver={driver.primary}",
        f"reconcile={reconcile.level}",
    ]
    if reconcile.caveats:
        evidence.append(f"conflict={reconcile.caveats[0]}")
    if timing.evidence:
        evidence.append(f"timing={timing.evidence[0]}")
    h_c = horizons.get("C")
    if h_c:
        evidence.append(f"range_p={h_c.range_probability:.2f}")
    verdict = ScenarioVerdict(
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
        reconcile_level=reconcile.level,
        reconcile_caveats=reconcile.caveats,
        factor_contributions=reconcile.factor_contributions,
    )
    try:
        from hunt_core.deep.verdict_v2.rr_audit import append_rr_geometry_audit

        append_rr_geometry_audit(row, plan=plan, verdict=verdict)
    except Exception:
        logger.exception("RR geometry audit failed")
    try:
        from hunt_core.deep.verdict_v2.evidence_trace import append_evidence_trace

        append_evidence_trace(row, verdict=verdict)
    except Exception:
        logger.exception("evidence trace append failed")
    try:
        append_reconcile_path_shadow(
            row,
            path=path,
            shadow_path=shadow_path,
            reconcile_level=reconcile.level,
            reconcile_conflicts=reconcile.conflicts,
            reconcile_caveats=reconcile.caveats,
            action=str(decision.action),
        )
    except Exception:
        logger.exception("reconcile path shadow append failed")
    return verdict

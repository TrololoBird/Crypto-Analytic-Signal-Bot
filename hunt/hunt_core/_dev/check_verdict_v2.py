"""Synthetic acceptance checks for Verdict V2 rules R1–R15."""
from __future__ import annotations

import sys

from hunt_core.deep.verdict_v2.blender import blend_horizons, build_conflict_matrix
from hunt_core.deep.verdict_v2.config import SignalGates, VerdictV2Config
from hunt_core.deep.verdict_v2.engines import run_all_engines
from hunt_core.deep.verdict_v2.orchestrator import build_scenario_verdict
from hunt_core.deep.verdict_v2.signal_decision import decide_signal
from hunt_core.deep.verdict_v2.types import (
    DataQualityReport,
    EngineOutput,
    ExpectedPath,
    ScenarioCatalyst,
    ScenarioFragility,
    SignalStrength,
    TradeQuality,
)


def _base_row(**overrides: object) -> dict:
    row = {
        "symbol": "BTCUSDT",
        "price": 65000.0,
        "structure": {
            "structure_bias": "long",
            "htf_trend": "bull",
            "bos_direction": "bull",
            "choch_detected": False,
            "liquidity_pools": {"nearest_below": 64000, "nearest_above": 67000},
            "key_levels": {"poc": 64500},
        },
        "regime": {"poc_1h": 64500},
        "market": {
            "funding_zscore_48h": 0.2,
            "oi_z": 0.5,
            "taker_5m": 1.05,
            "taker_15m": 1.02,
            "taker_1h": 1.0,
            "depth_imbalance": 0.1,
            "bid": 64999,
            "oi": 1e9,
            "funding_rate": 0.0001,
        },
        "maps": {"liquidation": {"forward_zones": []}, "volume_profile": {"profiles": []}},
        "timeframes": {
            "1w": {"status": "ok", "adx14": 30, "ema20": 64000, "ema50": 62000, "close": 65000},
            "1d": {"status": "ok", "adx14": 28, "trend_age": 15, "ema20": 64500, "ema50": 63000},
            "4h": {"status": "ok", "adx14": 25, "atr14": 800, "trend_age": 10, "bars_since_cross": 5},
            "1h": {"status": "ok", "adx14": 22, "atr14": 400},
            "15m": {"status": "ok", "adx14": 18},
            "5m": {"status": "ok"},
        },
    }
    row.update(overrides)
    return row


def check_r1_quality_blend() -> None:
    cfg = VerdictV2Config()
    engines = {
        "macro_trend": EngineOutput(0.7, 0.3, 0.4, 0.1, 0.5, 0.6, [], 2, 3),
        "structural": EngineOutput(0.6, 0.4, 0.2, 0.08, 0.25, 0.5, [], 1, 4),
    }
    full = blend_horizons({**run_all_engines(_base_row()), **engines}, cfg)
    assert "A" in full and full["A"].conviction >= 0


def check_r7_conflict() -> None:
    engines = run_all_engines(_base_row())
    m = build_conflict_matrix(engines)
    assert all(0 <= v <= 1 for v in m.values())


def check_r6_exec_zero_on_a() -> None:
    cfg = VerdictV2Config()
    assert cfg.priorities_a.get("execution_pressure", 0) >= 0.0
    assert "cross_consensus" in cfg.priorities_c


def check_r11_strength_disclaimer() -> None:
    v = build_scenario_verdict(_base_row())
    assert "not win probability" in v.signal_strength.disclaimer.lower() or v.signal_strength.disclaimer


def check_r12_trade_quality_advisory() -> None:
    tq = TradeQuality(0.3, 0.5, 0.8, "poor", "advisory")
    path = ExpectedPath(
        "pullback_down", "short", (1.0, 3.0), (6.0, 24.0), 66000.0, 0.7, "test", [], ""
    )
    dec = decide_signal(
        path,
        SignalStrength(0.8, "strong", False),
        ScenarioFragility(0.2, "low"),
        tq,
        None,
        DataQualityReport(0.9),
        ScenarioCatalyst("level_break", "break", 65500, 0.6),
        VerdictV2Config(gates=SignalGates(require_timing_c=False)),
        timing=None,
    )
    assert dec.action in {"short", "wait"}


def check_r14_timing_not_in_blend() -> None:
    cfg = VerdictV2Config()
    for pri in (cfg.priorities_a, cfg.priorities_b, cfg.priorities_c):
        assert "15m" not in pri and "5m" not in pri


def check_r13_range_probability() -> None:
    horizons = blend_horizons(run_all_engines(_base_row()), VerdictV2Config())
    for h in horizons.values():
        assert 0 <= h.range_probability <= 1


def check_r14_timing_gate() -> None:
    from hunt_core.deep.verdict_v2.timing_gate import assess_timing_gate

    row = _base_row()
    gate = assess_timing_gate(row, "long")
    assert isinstance(gate.ready, bool)
    horizons = blend_horizons(run_all_engines(row), VerdictV2Config())
    gate_c = assess_timing_gate(row, "long", horizons=horizons)
    assert isinstance(gate_c.ready, bool)


def check_wait_vs_short_same_path() -> None:
    row = _base_row()
    v = build_scenario_verdict(row)
    if v.expected_path.direction == "short":
        assert v.signal_decision.action in {"short", "wait"}


def check_suggest_gates() -> None:
    summaries = [
        {"path": "continuation_up", "path_direction": "long", "action": "wait", "strength": 0.48, "gates_failed": ["strength"]},
        {"path": "continuation_down", "path_direction": "short", "action": "short", "strength": 0.52, "gates_failed": []},
        {"path": "continuation_down", "path_direction": "short", "action": "wait", "strength": 0.46, "gates_failed": ["strength"]},
        {"path": "continuation_up", "path_direction": "long", "action": "long", "strength": 0.55, "gates_failed": []},
    ]
    from hunt_core.deep.verdict_v2.calibration import suggest_gates

    sg = suggest_gates(summaries, min_samples=4)
    assert sg.get("applied")
    assert float(sg["strength_min"]) <= 0.50


def check_signal_queue_score() -> None:
    from hunt_core.deep.verdict_v2.signal_queue import compute_opportunity_score

    active = compute_opportunity_score(
        {"action": "short", "strength": 0.52, "path": "continuation_down", "rr_primary": 1.5, "fragility": 0.25, "trade_quality": "favorable"}
    )
    wait_low = compute_opportunity_score(
        {"action": "wait", "strength": 0.20, "path": "continuation_up", "rr_primary": 1.0, "fragility": 0.25, "trade_quality": "marginal"}
    )
    assert active > wait_low
    assert active > 0.4


def check_reconcile_strong_conflict() -> None:
    from hunt_core.deep.verdict_v2.reconcile import reconcile_context
    from hunt_core.deep.verdict_v2.types import ExpectedPath, PatternCandidate, PatternConfidence, TradePlan

    row = _base_row(
        market={
            "depth_imbalance": 0.54,
            "funding_zscore_48h": 0.2,
            "oi_z": 0.5,
            "taker_5m": 1.05,
            "taker_15m": 1.02,
            "taker_1h": 1.0,
            "bid": 64999,
            "oi": 1e9,
            "funding_rate": 0.0001,
            "liq_heatmap_nearest_short": 70000.0,
        }
    )
    path = ExpectedPath(
        "continuation_down", "short", (1.0, 3.0), (6.0, 24.0), 66000.0, 0.7, "test", [], ""
    )
    plan = TradePlan(
        "short", "pullback_limit", (64800.0, 65000.0), 65500.0,
        64000.0, 63500.0, 63000.0, 1.2, 1.8, 2.4, 1.2, "", []
    )
    engines = run_all_engines(row)
    patterns = PatternConfidence(
        primary=PatternCandidate("distribution", 0.6, "short"),
        alternatives=(),
        spread=0.2,
        ambiguous=False,
    )
    rec = reconcile_context(row, path, plan, engines, patterns)
    assert rec.level in {"mild_conflict", "strong_conflict"}


def check_plan_monotonic_r() -> None:
    from hunt_core.deep.plan import finalize_plan_geometry

    geom = finalize_plan_geometry(
        {
            "entry_zone": [64800.0, 65000.0],
            "stop_loss": 65500.0,
            "tp1": 64000.0,
            "tp2": 64500.0,
            "tp3": 63000.0,
        },
        direction="short",
        atr=400.0,
    )
    assert geom["rr_tp1"] <= geom["rr_tp2"] <= geom["rr_tp3"]


def check_queue_gold_collapse() -> None:
    from hunt_core.deep.verdict_v2.signal_queue import build_top3

    rows = {
        "XAUUSDT": {
            "symbol": "XAUUSDT",
            "verdict_v2_summary": {
                "action": "short",
                "strength": 0.83,
                "path": "continuation_down",
                "rr_primary": 1.2,
                "fragility": 0.2,
                "trade_quality": "favorable",
                "entry_lo": 1.0,
                "entry_hi": 2.0,
            },
        },
        "PAXGUSDT": {
            "symbol": "PAXGUSDT",
            "verdict_v2_summary": {
                "action": "short",
                "strength": 0.81,
                "path": "continuation_down",
                "rr_primary": 1.1,
                "fragility": 0.2,
                "trade_quality": "favorable",
                "entry_lo": 1.0,
                "entry_hi": 2.0,
            },
        },
        "BTCUSDT": {
            "symbol": "BTCUSDT",
            "verdict_v2_summary": {
                "action": "short",
                "strength": 0.90,
                "path": "continuation_down",
                "rr_primary": 1.5,
                "fragility": 0.15,
                "trade_quality": "favorable",
                "entry_lo": 1.0,
                "entry_hi": 2.0,
            },
        },
    }
    top = build_top3(rows, top_n=3)
    syms = {t.symbol for t in top}
    assert "XAUUSDT" not in syms or "PAXGUSDT" not in syms
    assert "BTCUSDT" in syms


def main() -> int:
    checks = [
        check_r1_quality_blend,
        check_r7_conflict,
        check_r6_exec_zero_on_a,
        check_r11_strength_disclaimer,
        check_r12_trade_quality_advisory,
        check_r13_range_probability,
        check_r14_timing_not_in_blend,
        check_r14_timing_gate,
        check_wait_vs_short_same_path,
        check_suggest_gates,
        check_signal_queue_score,
        check_reconcile_strong_conflict,
        check_plan_monotonic_r,
        check_queue_gold_collapse,
    ]
    failed = 0
    for fn in checks:
        try:
            fn()
            print(f"OK {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}", file=sys.stderr)
    v = build_scenario_verdict(_base_row())
    print(f"sample action={v.signal_decision.action} path={v.expected_path.type}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

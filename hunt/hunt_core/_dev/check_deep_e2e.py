"""End-to-end Deep Module-1 assertions (plan abstract-chasing-cerf R1–R11)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from hunt_core.deep.format_telegram import format_deep_from_row
from hunt_core.deep.plan import finalize_plan_geometry
from hunt_core.deep.verdict_v2.orchestrator import build_scenario_verdict
from hunt_core.deep.verdict_v2.path_mapper import map_to_expected_path
from hunt_core.deep.verdict_v2.patterns import generate_patterns
from hunt_core.deep.verdict_v2.config import VerdictV2Config
from hunt_core.deep.verdict_v2.horizon_topology import classify_topology
from hunt_core.deep.verdict_v2.signal_queue import build_top3, format_queue_telegram
from hunt_core.deep.verdict_v2.blender import blend_horizons
from hunt_core.deep.verdict_v2.engines import run_all_engines
from hunt_core.deep.verdict_v2.market_driver import infer_driver
from hunt_core.deep.verdict_v2.features.maturity import extract_maturity
from hunt_core.deep.verdict_v2.context import classify_market_context


def _neutral_row(**overrides: object) -> dict[str, Any]:
    row = {
        "symbol": "ETHUSDT",
        "price": 3500.0,
        "as_of": "2026-06-22T12:00:00+00:00",
        "freshness": {"as_of": "2026-06-22T12:00:00+00:00", "dom_age_s": 5.0},
        "structure": {
            "structure_bias": "wait",
            "htf_trend": "neutral",
            "bos_direction": "",
            "choch_detected": False,
            "liquidity_pools": {},
            "key_levels": {"poc": 3480},
        },
        "regime": {"poc_1h": 3480},
        "market": {
            "depth_imbalance": 0.0,
            "funding_zscore_48h": 0.0,
            "oi_z": 0.0,
            "taker_5m": 1.0,
            "taker_15m": 1.0,
            "taker_1h": 1.0,
            "bid": 3499,
            "oi": 1e9,
            "funding_rate": 0.0001,
            "liq_synthetic_only": True,
            "liq_realized_events": 0,
        },
        "maps": {"liquidation": {"forward_zones": []}, "volume_profile": {"profiles": []}},
        "timeframes": {
            "1w": {"status": "ok", "adx14": 12, "ema20": 3500, "ema50": 3500, "close": 3500},
            "1d": {"status": "ok", "adx14": 12, "ema20": 3500, "ema50": 3500, "close": 3500},
            "4h": {"status": "ok", "adx14": 12, "atr14": 40, "close": 3500},
            "1h": {"status": "ok", "adx14": 12, "atr14": 20, "close": 3500},
            "15m": {"status": "ok", "adx14": 12},
            "5m": {"status": "ok"},
        },
    }
    row.update(overrides)
    return row


def _conflict_row() -> dict[str, Any]:
    return _neutral_row(
        symbol="BTCUSDT",
        price=65000.0,
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
            "liq_synthetic_only": True,
            "liq_realized_events": 0,
        },
        structure={
            "structure_bias": "short",
            "htf_trend": "bear",
            "bos_direction": "bear",
            "choch_detected": False,
            "liquidity_pools": {"nearest_above": 66000, "nearest_below": 64000},
            "key_levels": {"poc": 64500},
        },
    )


def check_no_continuation_on_neutral() -> None:
    cfg = VerdictV2Config()
    row = _neutral_row()
    engines = run_all_engines(row)
    horizons = blend_horizons(engines, cfg)
    topo = classify_topology(horizons)
    maturity = extract_maturity(row)
    ctx = classify_market_context(row)
    driver = infer_driver([], "")
    patterns = generate_patterns(row, topo, maturity, ctx, driver, cfg)
    path = map_to_expected_path(row, patterns, topo)
    assert not path.type.startswith("continuation"), path.type


def check_plan_monotonic_r() -> None:
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


def check_as_of_in_render() -> None:
    row = _neutral_row()
    row["verdict_v2"] = build_scenario_verdict(row)
    text = format_deep_from_row(row)
    assert "снимок" in text or "2026-06-22" in text


def check_conflict_caveat_or_wait() -> None:
    row = _conflict_row()
    v = build_scenario_verdict(row)
    assert v.reconcile_level in {"mild_conflict", "strong_conflict", "coherent"}
    if v.reconcile_level != "coherent":
        assert v.reconcile_caveats or v.signal_decision.action == "wait"


def check_queue_gold_and_labels() -> None:
    rows = {
        "BTCUSDT": {
            "symbol": "BTCUSDT",
            "verdict_v2_summary": {
                "action": "short",
                "strength": 0.90,
                "path": "pullback_down",
                "rr_primary": 1.5,
                "fragility": 0.15,
                "trade_quality": "favorable",
                "entry_lo": 1.0,
                "entry_hi": 2.0,
            },
        },
        "XAUUSDT": {
            "symbol": "XAUUSDT",
            "verdict_v2_summary": {
                "action": "short",
                "strength": 0.83,
                "path": "pullback_down",
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
                "path": "pullback_down",
                "rr_primary": 1.1,
                "fragility": 0.2,
                "trade_quality": "favorable",
                "entry_lo": 1.0,
                "entry_hi": 2.0,
            },
        },
    }
    top = build_top3(rows, top_n=3)
    syms = {t.symbol for t in top}
    assert "BTCUSDT" in syms
    assert not ("XAUUSDT" in syms and "PAXGUSDT" in syms)
    tg = format_queue_telegram({"top3": [t.__dict__ if hasattr(t, "__dict__") else t for t in top]})
    assert "приоритет" in tg.lower()
    assert "ранг" not in tg.lower() or "не вероятность" in tg.lower()


def check_primary_not_alt_pattern() -> None:
    cfg = VerdictV2Config()
    row = _neutral_row(
        structure={
            "structure_bias": "short",
            "htf_trend": "bear",
            "bos_direction": "bear",
            "choch_detected": False,
            "liquidity_pools": {},
            "key_levels": {"poc": 3480},
        },
        market={
            "funding_zscore_48h": 1.5,
            "oi_z": 1.2,
            "depth_imbalance": -0.2,
            "taker_5m": 0.95,
            "taker_15m": 0.96,
            "taker_1h": 0.97,
            "bid": 3499,
            "oi": 1e9,
            "funding_rate": 0.001,
        },
    )
    engines = run_all_engines(row)
    horizons = blend_horizons(engines, cfg)
    topo = classify_topology(horizons)
    maturity = extract_maturity(row)
    ctx = classify_market_context(row)
    driver = infer_driver([], "")
    patterns = generate_patterns(row, topo, maturity, ctx, driver, cfg)
    alts = {a.id for a in patterns.alternatives}
    assert patterns.primary.id not in alts


def check_activation_event_deduped() -> None:
    from hunt_core.deep.verdict_v2.serialize import attach_verdict_v2_to_row
    from hunt_core.deep.verdict_v2.orchestrator import build_scenario_verdict

    row = _neutral_row(
        price=3490.0,
        structure={
            "structure_bias": "short",
            "htf_trend": "bear",
            "bos_direction": "bear",
            "choch_detected": False,
            "liquidity_pools": {"nearest_above": 3550, "nearest_below": 3400},
            "key_levels": {"poc": 3480},
        },
    )
    row["verdict_v2"] = build_scenario_verdict(row)
    attach_verdict_v2_to_row(row)
    evt1 = (row.get("verdict_v2_summary") or {}).get("activation_event")
    assert evt1 is not None, "expected activation on zone entry"
    row["verdict_v2_summary"]["plan_lifecycle"] = "active"
    attach_verdict_v2_to_row(row)
    evt2 = (row.get("verdict_v2_summary") or {}).get("activation_event")
    assert evt2 == evt1, "activation must be one-shot deduped"


async def _live_btc_assemble_e2e() -> None:
    """Plan E2E: assemble_deep_tick(BTCUSDT) → format_deep_from_row."""
    from hunt_core.deep.format_telegram import format_deep_from_row
    from hunt_core.deep.verdict_v2.serialize import ensure_verdict_v2
    from hunt_core.deep.verdict_v2.types import ScenarioVerdict
    from hunt_core.domain.config import load_settings
    from hunt_core.market.factory import create_hunt_market_plane_from_settings
    from hunt_core.runtime.deep_assembly import assemble_deep_tick

    settings = load_settings()
    plane = await create_hunt_market_plane_from_settings(settings)
    try:
        row = await assemble_deep_tick("BTCUSDT", plane.client, stagger_ms=200)
    finally:
        await plane.close()
    if row.get("error"):
        raise RuntimeError(f"assemble_deep_tick failed: {row['error']}")
    ensure_verdict_v2(row)
    v2 = row.get("verdict_v2")
    assert isinstance(v2, ScenarioVerdict), "missing ScenarioVerdict"
    plan = v2.trade_plan
    if plan and v2.signal_decision.action in {"long", "short"}:
        rrs = [plan.rr_tp1, plan.rr_tp2, plan.rr_tp3]
        assert rrs[0] <= rrs[1] <= rrs[2], f"TP ladder R not monotonic: {rrs}"
    text = format_deep_from_row(row)
    assert row.get("as_of") or (row.get("freshness") or {}).get("as_of"), "missing as_of"
    assert "снимок" in text or str(row.get("as_of", ""))[:10] in text, "as_of not in render"
    low = text.lower()
    assert "сила" in low or "сила сигнала" in low
    assert "приоритет" not in low or "очеред" in low
    if "continuation" in (v2.expected_path.type or "") and str(
        (row.get("structure") or {}).get("htf_trend") or ""
    ).lower() in {"", "neutral", "wait"}:
        raise AssertionError("continuation on neutral structure")
    print("OK live_btc_assemble_e2e", v2.signal_decision.action, v2.expected_path.type)


def check_live_btc_assemble_e2e() -> None:
    asyncio.run(_live_btc_assemble_e2e())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="run assemble_deep_tick BTCUSDT smoke")
    args = parser.parse_args()
    checks = [
        check_no_continuation_on_neutral,
        check_plan_monotonic_r,
        check_as_of_in_render,
        check_conflict_caveat_or_wait,
        check_queue_gold_and_labels,
        check_primary_not_alt_pattern,
        check_activation_event_deduped,
    ]
    if args.live or os.environ.get("HUNT_LIVE") == "1":
        checks.append(check_live_btc_assemble_e2e)
    failed = 0
    for fn in checks:
        try:
            fn()
            print(f"OK {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

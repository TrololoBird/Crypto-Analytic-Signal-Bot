"""Offline logic self-checks — replacement for removed verify CLI (P11/E1)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

from hunt_core.contract import validate_signal_contract
from hunt_core.confluence.confluence import evaluate_must_pass, family_vote_count, FAMILY_VOTE_MIN
from hunt_core.confluence.mtf import MTFConfluence, ScenarioScore
from hunt_core.domain.config import load_config_defaults_toml, load_toml_defaults
from hunt_core.levels.levels import MIN_RR
from hunt_core.regime.classifier import Regime


def _sample_signal() -> SimpleNamespace:
    return SimpleNamespace(
        direction="short",
        entry_low=100.0,
        entry_high=101.0,
        stop_loss=105.0,
        tp1=95.0,
        tp2=90.0,
        tp3=90.0,
        scale_weights=(0.5, 0.5),
        valid_until=datetime.now(UTC) + timedelta(hours=12),
    )


def main() -> int:
    issues: list[str] = []

    sig = _sample_signal()
    contract_issues = validate_signal_contract(sig, min_risk_reward=1.0)
    if contract_issues:
        issues.append(f"contract expected pass got {contract_issues}")

    defaults = load_toml_defaults()
    if not isinstance(defaults, dict):
        issues.append("load_toml_defaults not dict")

    row = {
        "lifecycle": {"recommended_bias": "long"},
        "dump": {"dump_score": 80, "confirmed": True},
    }
    ok, missing = evaluate_must_pass(row, direction="short")
    if ok or "htf_bias_veto" not in missing:
        issues.append(f"must_pass veto expected got ok={ok} missing={missing}")

    mtf = MTFConfluence(
        symbol="T",
        price=1.0,
        tf_signals={},
        long_scenario=ScenarioScore("long", 0.4, 0, 3, 0, 0, 0, 0, 0),
        short_scenario=ScenarioScore("short", 0.5, 0, 3, 0, 0, 0, 0, 0),
        dominant="neutral",
    )
    if family_vote_count(mtf, direction="short") >= FAMILY_VOTE_MIN:
        issues.append("family_vote low fixture expected")

    bad_rr = _sample_signal()
    bad_rr.stop_loss = 100.5
    bad_issues = validate_signal_contract(bad_rr, min_risk_reward=1.5)
    if not bad_issues:
        issues.append("contract expected RR fail")

    if MIN_RR < 1.5:
        issues.append(f"levels MIN_RR expected >=1.5 got {MIN_RR}")

    from hunt_core.contract import parse_liquidation_score
    from hunt_core.deliver.dispatch import _contract_issues_for_setup, format_delivery_card

    if parse_liquidation_score(-1.0) is not None:
        issues.append("parse_liquidation_score must reject -1.0 sentinel")
    if parse_liquidation_score(0.25) != 0.25:
        issues.append("parse_liquidation_score must accept [0,1] values")

    # Legacy dump_analysis scoring removed — fusion engine owns detection fuel.
    row = {"symbol": "TESTUSDT", "price": 1.05, "lifecycle": {"phase": "dump_confirmed"}, "market": {}}
    setup = {
        "entry_zone": [1.0, 1.02],
        "stop_loss": 1.06,
        "tp1": 0.97,
        "tp2": 0.94,
        "dump_fuel": 80,
        "risk_reward": 1.8,
    }
    card = format_delivery_card(row, direction="short", setup=setup, delivery_tier="armed")
    if "conviction" not in card.lower():
        issues.append("delivery card must show conviction score")
    if "худший fill" not in card.lower() and "худший" not in card.lower():
        issues.append("delivery card must show worst-fill label")
    if "ORDER FLOW" in card:
        issues.append("delivery card must omit order-flow block without inputs")
    if "(+4.9%)" not in card:
        issues.append("delivery card TP% should anchor to entry edge not current price")

    armed_row = {
        "symbol": "TESTUSDT",
        "price": 1.08,
        "lifecycle": {"phase": "distribution", "recommended_bias": "short"},
        "market": {},
    }
    armed_setup = {
        "entry_zone": [1.0, 1.02],
        "stop_loss": 1.06,
        "tp1": 0.97,
        "tp2": 0.94,
        "dump_fuel": 76,
        "early_tier": "armed",
        "anticipation": True,
        "risk_reward": 1.8,
    }
    armed_card = format_delivery_card(
        armed_row, direction="short", setup=armed_setup, delivery_tier="armed"
    )
    if "(+4.9%)" not in armed_card:
        issues.append("armed card TP% must use worst entry edge not spot price")
    if "Причины" not in armed_card:
        issues.append("armed card must include plain reasons block")
    if "ARMED" not in armed_card:
        issues.append("armed card must label ARMED tier")

    from hunt_core.deliver.dispatch import _for_against
    from hunt_core.deliver._labels import trigger_human

    if trigger_human("pp_short_break") != "пробой support (pp)":
        issues.append("trigger_human must map pp_short_break")
    short_row = {
        "lifecycle": {"phase": "dump_active", "recommended_bias": "wait"},
        "market": {},
    }
    short_setup = {
        "confirm_hard": ["1m_close_below_support", "pp_short_break"],
        "dump_fuel": 80,
    }
    pros, cons = _for_against(short_row, direction="short", setup=short_setup)
    if not any("support" in p.lower() or "пробой" in p for p in pros):
        issues.append("short close_below_support must be pro not con")
    if any("bias=wait" in c for c in cons):
        issues.append("bias wait label must be humanized")

    from hunt_core.levels.levels import structural_long_levels

    absurd_long = structural_long_levels(
        price=0.75,
        impulse_high=0.80,
        impulse_low=0.60,
        fib={"ext_1272": 1.10, "ret_382": 0.70},
        atr15=0.01,
        local_support=0.72,
        local_resistance=1.05,
        lifecycle_phase="impulse_initiating",
    )
    try:
        tp1_abs = float(absurd_long.get("tp1") or 0)
        worst = float((absurd_long.get("entry_zone") or [0.74, 0.76])[0])
        if tp1_abs > worst * 1.16:
            issues.append(f"long TP1 must respect TP1_MAX_PCT cap got {tp1_abs} vs {worst}")
    except (TypeError, ValueError):
        issues.append("structural_long_levels TP1 cap fixture failed")

    repair_setup = {
        "confirmed": True,
        "entry_zone": [0.98, 1.0],
        "stop_loss": 1.02,
        "tp1": 0.99,
        "tp2": 0.97,
        "impulse_low": 0.95,
    }
    repair_issues = _contract_issues_for_setup(
        direction="short",
        setup=repair_setup,
        min_risk_reward=1.15,
    )
    if not repair_issues:
        issues.append("low-RR setup expected contract fail without TP fabrication")

    if Regime.RANGE.value != "range":
        issues.append("regime enum drift")

    if not load_config_defaults_toml():
        issues.append("config.defaults.toml empty or missing")

    from hunt_core.scanner.gate._ev import delivery_ev_floors, resolve_delivery_ev
    from hunt_core.scanner.gate._policy_decl import _decl_check_ev_delivery

    good_setup = {
        "confirmed": True,
        "entry_zone": [1.0, 1.01],
        "stop_loss": 1.05,
        "tp1": 0.95,
        "delivery_ev": 0.02,
        "delivery_p_win": 0.55,
    }
    blocked = _decl_check_ev_delivery(
        row={"symbol": "TESTUSDT", "market": {}, "structure": {}},
        setup=dict(good_setup),
        direction="short",
        lifecycle={"phase": "distribution"},
        delivery_tier="triggered",
        symbol="TESTUSDT",
    )
    if blocked is not None:
        issues.append(f"EV delivery gate must pass good setup got {blocked.code}")

    bad_ev = dict(good_setup)
    bad_ev["delivery_ev"] = -0.01
    blocked_neg = _decl_check_ev_delivery(
        row={"symbol": "TESTUSDT", "market": {}, "structure": {}},
        setup=bad_ev,
        direction="short",
        lifecycle={"phase": "distribution"},
        delivery_tier="triggered",
        symbol="TESTUSDT",
    )
    if blocked_neg is None or blocked_neg.code != "ev_below_floor":
        issues.append(f"EV delivery must block negative EV got {blocked_neg}")

    _min_ev, min_p = delivery_ev_floors("TESTUSDT", confirmed=True)
    if min_p < 0.4:
        issues.append(f"delivery min_p_win expected >=0.4 got {min_p}")

    resolved = resolve_delivery_ev(
        {
            "ev_primary_ev": 0.03,
            "p_win": 0.5,
            "entry_zone": [1.0, 1.01],
            "stop_loss": 1.05,
            "tp1": 0.95,
        },
        direction="short",
    )
    if resolved.get("ev") != 0.03 or resolved.get("p_win") != 0.5:
        issues.append(f"resolve_delivery_ev primary fields got {resolved}")

    from hunt_core.scanner.gate._ev import setup_meets_strength

    weak_dump = {"dump_fuel": 30, "confirm_hard": ["5m_rejection", "close_below_support"]}
    strong_dump = {"delivery_p_win": 0.55, "confirm_hard": ["5m_rejection", "close_below_support"]}
    if setup_meets_strength(weak_dump, direction="short", symbol="TESTUSDT", tier="confirm"):
        issues.append("confirm strength must reject low shadow fuel without P(win)")
    if not setup_meets_strength(strong_dump, direction="short", symbol="TESTUSDT", tier="confirm"):
        issues.append("confirm strength must pass when delivery_p_win >= min_p_win")

    from hunt_core.scanner.gate._rr import (
        apply_structure_ev_fuel_cap,
        structure_ev_fuel_cap,
    )

    bad_rr = {
        "levels_viable": True,
        "entry_zone": [1.0, 1.01],
        "stop_loss": 1.05,
        "tp1": 0.995,
        "phase": "dump_active",
    }
    if structure_ev_fuel_cap(bad_rr, direction="short") > 45:
        issues.append("low RR short setup must cap structure fuel <=45")
    capped_setup = dict(bad_rr)
    capped = apply_structure_ev_fuel_cap(88.0, capped_setup, direction="short")
    if capped >= 88.0:
        issues.append("apply_structure_ev_fuel_cap must reduce high fuel on bad RR")
    if capped_setup.get("structure_ev_cap") is None:
        issues.append("structure cap must stamp structure_ev_cap when applied")

    from hunt_core.deliver.dispatch import _squeeze_note

    sq = _squeeze_note(
        {"lifecycle": {}, "market": {"funding_pct": -0.0896, "funding_rate": -0.0008956}},
        direction="short",
    )
    if sq and "-8.9" in sq:
        issues.append(f"squeeze note must not 100x funding display got {sq}")
    if sq and "-0.090%" not in sq and "-0.089%" not in sq:
        issues.append(f"squeeze note expected ~-0.09% funding got {sq}")

    from hunt_core.levels.levels import _phase_min_rr_short

    if _phase_min_rr_short("exhaustion_at_high") < 2.0:
        issues.append("exhaustion_at_high levels min RR must be >=2.0")

    from hunt_core.data.completeness import delivery_derivatives_complete

    ok_deriv, deriv_missing = delivery_derivatives_complete(
        {
            "market": {
                "oi": 1_000_000.0,
                "oi_chg_1h": 0.5,
                "taker_5m": 0.55,
                "taker_1h": 0.52,
                "top_ls_5m": 1.1,
                "global_ls_5m": 1.05,
            }
        },
        tier="fast",
    )
    if ok_deriv:
        issues.append("fast-tier missing funding must fail delivery_derivatives_complete")
    elif not any("funding" in item for item in deriv_missing):
        issues.append(f"expected funding violation got {deriv_missing}")

    from hunt_core.features.shared import wilder_mean
    from hunt_core.features.polars_ta_bridge import rsi_series

    import polars as pl

    s = pl.Series([float(i) for i in range(1, 50)])
    w = wilder_mean(s, period=14, name="parity")
    if w.null_count() >= len(s):
        issues.append("wilder_mean produced all-null series")
    df = pl.DataFrame({"close": s})
    ta_rsi = rsi_series(df, period=14)
    ta_vals = [float(v) for v in ta_rsi.to_list() if v is not None]
    if len(ta_vals) < 5:
        issues.append("rsi_series too few finite values")
    elif not (0.0 <= ta_vals[-1] <= 100.0):
        issues.append(f"rsi_series out of [0,100] got {ta_vals[-1]}")

    from hunt_core.market.factory import resample_ohlcv_from_1m

    import ccxt

    ex = ccxt.binance()
    from datetime import UTC, datetime, timedelta

    start = datetime(2024, 1, 1, tzinfo=UTC)
    rows: list[dict[str, float | int | datetime]] = []
    for i in range(300):
        o_ms = int((start + timedelta(minutes=i)).timestamp() * 1000)
        c_ms = o_ms + 60_000 - 1
        o = 100.0 + i * 0.02
        rows.append(
            {
                "time": o_ms,
                "open": o,
                "high": o + 0.5,
                "low": o - 0.5,
                "close": o + 0.1,
                "volume": 10.0,
                "close_time": c_ms,
                "quote_volume": 0.0,
                "num_trades": 0,
                "taker_buy_base_volume": 0.0,
                "taker_buy_quote_volume": 0.0,
                "open_time": start + timedelta(minutes=i),
            }
        )
    df_1m = pl.DataFrame(rows)
    df_5m = resample_ohlcv_from_1m(df_1m, "5m", exchange=ex, limit=24)
    if df_5m.height < 3:
        issues.append(f"resample_ohlcv_from_1m expected >=3 5m bars got {df_5m.height}")

    # #43 fail-loud budget: critical modules must not grow silent except-pass swallows.
    import re
    from pathlib import Path

    hunt_root = Path(__file__).resolve().parents[1]
    swallow_re = re.compile(r"except\s+[^:]+:\s*\n(?:[^\n]*\n)*?\s+pass\b", re.MULTILINE)
    swallow_budget: dict[str, int] = {
        "deliver/dispatch.py": 0,
        "scanner/detect/delivery_support.py": 0,
        "scanner/gate/_strategic.py": 0,
        "scanner/gate/_quality.py": 0,
        "scanner/gate/_mission.py": 0,
        "scanner/gate/_registry.py": 0,
        "runtime/tick_assembly.py": 0,
        "signals/emit.py": 0,
        "data_readiness.py": 0,
        "scanner/gate/_ev.py": 0,
        "scanner/gate/_delivery_helpers.py": 4,
        "features/snapshot.py": 8,
    }
    for rel, budget in swallow_budget.items():
        path = hunt_root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        count = len(swallow_re.findall(text))
        if count > budget:
            issues.append(f"#43 {rel}: silent except-pass {count} > budget {budget}")

    from hunt_core.scanner.gate.delivery import collect_report_blockers, run_gate_pipeline

    row = {"symbol": "TESTUSDT", "price": 1.0, "market": {}, "session": {}}
    setup = {"confirmed": False, "dump_score": 50.0}
    lc = {"phase": "dump_active"}
    report_codes = {
        b.code
        for b in collect_report_blockers(
            setup, direction="short", symbol="TEST", row=row, lifecycle=lc
        )
    }
    gate = run_gate_pipeline(
        direction="short",
        setup=setup,
        row=row,
        lifecycle=lc,
        symbol="TEST",
    )
    if gate.code not in report_codes and gate.code != "ok":
        issues.append(
            f"run_gate_pipeline vs collect_report_blockers drift: live={gate.code} report={sorted(report_codes)}"
        )

    from hunt_core.scanner.gate.delivery import delivery_freshness_block, price_in_entry_zone

    home_setup = {"entry_zone": [0.035201, 0.03572], "tp1": 0.032459}
    if price_in_entry_zone(home_setup, 0.03574, direction="short"):
        issues.append("HOME chase: price above zone_hi must not be in zone for short")
    if not price_in_entry_zone(home_setup, 0.03571, direction="short"):
        issues.append("price_in_entry_zone must accept price inside band")
    stale_above = delivery_freshness_block(
        direction="short",
        setup=home_setup,
        row={"price": 0.03574},
    )
    if stale_above != "delivery_short_above_entry_zone":
        issues.append(f"short above entry zone expected block got {stale_above}")

    from hunt_core.deliver._brief import _entry_zones_overlap

    if not _entry_zones_overlap(
        {"entry_zone": [1.0, 1.02]},
        {"entry_zone": [1.0001, 1.0201]},
    ):
        issues.append("entry zone overlap detector expected match")
    if _entry_zones_overlap(
        {"entry_zone": [1.0, 1.02]},
        {"entry_zone": [0.9, 0.95]},
    ):
        issues.append("entry zone overlap detector expected no match")

    import inspect
    from hunt_core.data import collect as collect_mod

    src = inspect.getsource(collect_mod.resolve_kline_map)
    if 'n not in {"1m", "1w"}' not in src:
        issues.append("resolve_kline_map must resample full tier from 1m (X8)")
    if "limits[name] // 4" in src:
        issues.append("resolve_kline_map must not accept short resample (limits//4)")
    if "required_bars = int(limits[name])" not in src:
        issues.append("resolve_kline_map must require full kline limit before resample accept")

    from hunt_core.scanner.detect.market_cycle import cusum_series, detect_pump_cycle_events

    z = pl.Series([0.0, 1.0, 2.0, 1.5, 0.5])
    cus = cusum_series(z, threshold=1.0)
    if cus.len() != 5 or not all(isinstance(v, (int, float)) for v in cus.to_list()):
        issues.append("cusum_series must return numeric series (no Polars Expr leak)")

    cycle = detect_pump_cycle_events(
        pl.DataFrame({"close": [100.0, 101.0, 102.0, 101.5, 100.5, 99.0, 98.0, 99.0]})
    )
    if not isinstance(cycle.get("cusum"), (int, float)):
        issues.append("detect_pump_cycle_events must return numeric cusum")

    from hunt_core.scanner.detect.market_cycle import btc_decoupled_flags

    sym_1m = pl.DataFrame({"close": [100.0, 101.0, 102.5, 104.0, 105.0, 106.0, 107.0, 108.0]})
    btc_1m = pl.DataFrame({"close": [100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 100.6, 100.7]})
    dec = btc_decoupled_flags(sym_1m, btc_1m, beta=1.0, threshold=0.5)
    if not isinstance(dec.get("pump"), bool) or not isinstance(dec.get("dump"), bool):
        issues.append("btc_decoupled_flags must return pump/dump booleans on 1m frames")

    tick_src = inspect.getsource(
        __import__(
            "hunt_core.runtime.tick_assembly",
            fromlist=["snapshot_symbol"],
        ).snapshot_symbol
    )
    dec_block = tick_src.split("btc_decoupled_flags", 1)[-1][:400]
    if "prepared.work_1h" in dec_block:
        issues.append("btc_decoupled must use prepared.work_1m not work_1h (Phase C1)")

    from hunt_core.scanner.gate._delivery_helpers import (
        inject_kline_flow_into_market,
        kline_bar_flow,
        resolve_flow_cvd_px,
        ws_cvd_divergence_fuel_triggers,
    )

    delta, px = kline_bar_flow(
        {
            "5m_closed": {
                "open": 100.0,
                "close": 101.0,
                "session_cvd": 5000.0,
                "session_cvd_prev": 4000.0,
            }
        },
        "5m",
    )
    if delta != 1000.0 or px is None or abs(px - 1.0) > 0.01:
        issues.append(f"kline_bar_flow expected delta=1000 px=1% got {delta} {px}")

    mkt: dict[str, Any] = {}
    tf_fixture = {
        "5m_closed": {
            "open": 10.0,
            "close": 10.5,
            "session_cvd": 50.0,
            "session_cvd_prev": 200.0,
        }
    }
    inject_kline_flow_into_market(mkt, tf_fixture)
    if mkt.get("kline_cvd_delta_5m") != -150.0:
        issues.append("inject_kline_flow must stamp kline_cvd_delta_5m")
    triggers = ws_cvd_divergence_fuel_triggers(mkt, direction="short", tf=tf_fixture)
    if not any(t[0].startswith("kline_cvd_bear_div") for t in triggers):
        issues.append(f"kline CVD divergence expected kline trigger got {triggers}")
    _cvd, _px, src = resolve_flow_cvd_px(mkt, tf_fixture, interval="5m")
    if src != "kline":
        issues.append(f"resolve_flow_cvd_px must use kline fallback got src={src}")

    from hunt_core.features.snapshot import stamp_derivative_zscores

    z_mkt: dict[str, Any] = {}
    series = [float(100 + i) for i in range(20)]
    stamp_derivative_zscores(
        z_mkt,
        pack={"oi_series": series, "gls_series": series},
    )
    if z_mkt.get("oi_z") is None or z_mkt.get("gls_z") is None:
        issues.append("stamp_derivative_zscores must set oi_z and gls_z from series")

    basis_mkt: dict[str, Any] = {}
    stamp_derivative_zscores(
        basis_mkt,
        pack={"basis_5m": 0.85},
        ws_snap={"basis_ap_bps": 95.0, "mark_live": 1.01},
    )
    if basis_mkt.get("basis_ap_bps") != 95.0:
        issues.append(f"basis_ap_bps from ws expected 95 got {basis_mkt.get('basis_ap_bps')}")
    if basis_mkt.get("basis_pct") != 0.85:
        issues.append(f"basis_pct from pack expected 0.85 got {basis_mkt.get('basis_pct')}")

    ok_fast, miss_fast = delivery_derivatives_complete(
        {
            "market": {
                "oi": 1_000_000.0,
                "oi_chg_1h": 0.5,
                "funding": 0.0001,
                "taker_5m": 1.05,
                "taker_1h": 1.02,
                "top_ls_5m": 1.1,
                "global_ls_5m": 1.05,
                "oi_z": -1.8,
                "gls_z": 1.6,
                "basis_5m": 0.42,
                "basis_pct": 0.42,
            }
        },
        tier="fast",
    )
    if not ok_fast:
        issues.append(f"fast tier with z-scores must pass delivery_derivatives_complete got {miss_fast}")

    squeeze_row = {
        "lifecycle": {"recommended_bias": "short", "phase": "exhaustion_at_high"},
        "dump": {"dump_score": 93, "confirmed": True},
        "timeframes": {},
        "market": {"funding_rate": -0.0025},
    }
    ok_sq, miss_sq = evaluate_must_pass(squeeze_row, direction="short")
    if ok_sq or "mtf_funding_squeeze_short" not in miss_sq:
        issues.append(f"crowded-short funding must fail must_pass got ok={ok_sq} missing={miss_sq}")

    caution_row = {
        "lifecycle": {"recommended_bias": "short", "phase": "exhaustion_at_high"},
        "dump": {"dump_score": 93, "confirmed": True},
        "timeframes": {
            "5m_closed": {"closed_bar": True, "close": 1.0},
            "15m_closed": {"closed_bar": True, "close": 1.0},
        },
        "market": {"funding_rate": -0.0018},
    }
    ok_caution, miss_caution = evaluate_must_pass(caution_row, direction="short")
    if not ok_caution and "mtf_funding_squeeze_short" in miss_caution:
        issues.append(f"funding caution tier must not hard-block must_pass got {miss_caution}")
    from hunt_core.scanner.gate.policy import funding_short_risk_tier

    if funding_short_risk_tier(-0.0025) != "block":
        issues.append("funding_short_risk_tier -0.25% must be block")
    if funding_short_risk_tier(-0.0018) != "caution":
        issues.append("funding_short_risk_tier -0.18% must be caution")

    from hunt_core.scanner.gate.delivery import effective_min_rr_for_delivery

    exhaust_rr = effective_min_rr_for_delivery(
        {"confirmed": True},
        direction="short",
        symbol="HOMEUSDT",
        lifecycle={"phase": "exhaustion_at_high"},
    )
    if exhaust_rr < 2.0:
        issues.append(f"exhaustion_at_high delivery min RR must be >=2.0 got {exhaust_rr}")

    from hunt_core.shared.facts.order_flow import cvd_from_row as _cvd_from_row

    kline_row = {
        "timeframes": {
            "5m_closed": {
                "open": 1.0,
                "close": 0.99,
                "session_cvd": -5000.0,
                "session_cvd_prev": -3000.0,
            }
        },
        "market": {},
    }
    k_cur, k_prev = _cvd_from_row(kline_row)
    if k_cur is None:
        issues.append("kline CVD fallback must resolve session_cvd from closed block")

    # Legacy anticipation routing removed with fusion cutover.
    from hunt_core.runtime.query_service import build_query_result, format_query_telegram

    forming_row = {
        "symbol": "TESTUSDT",
        "price": 1.0,
        "lifecycle": {"phase": "distribution", "recommended_bias": "short", "short_entry_ok": True},
        "dump": {"dump_score": 55, "dump_fuel": 55, "phase": "dump_setup_forming", "confirmed": False},
        "long": {"long_score": 20},
        "timeframes": {},
    }
    q = build_query_result(
        forming_row, "TESTUSDT", source="tick_store", from_store=True, age_s=5.0
    )
    if q.focus_direction != "short":
        issues.append("query focus must prefer short on distribution forming")
    tg = format_query_telegram(q)
    if "Глубокий анализ" not in tg:
        issues.append("format_query_telegram must lead with deep analysis block")
    if "Pre-dump" in tg or "Manipulation fusion" in tg:
        issues.append("format_query_telegram must not expose watch fusion/predump narrative")

    from hunt_core.scanner.setups.detectors import detect_cex_dump, detect_cex_pump

    pump_row = {
        "price": 1.025,
        "market": {"agg_trade_buy_ratio_60s": 0.70},
        "lifecycle": {"phase": "impulse_initiating"},
    }
    pump_prep = {
        "timeframes": {
            "1m_closed": {
                "closed_bar": True,
                "close": 1.025,
                "vol_ratio": 3.5,
                "candle": {"open": 1.0, "close": 1.025},
            }
        },
        "market": pump_row["market"],
    }
    pump_hit = detect_cex_pump(pump_row, pump_prep)
    if pump_hit is None or pump_hit.direction != "long":
        issues.append("detect_cex_pump triple-gate fixture must fire long")
    dump_hit = detect_cex_dump(
        {
            "price": 0.975,
            "market": {"agg_trade_buy_ratio_60s": 0.30},
            "lifecycle": {"phase": "dump_active"},
        },
        {
            "timeframes": {
                "1m_closed": {
                    "closed_bar": True,
                    "close": 0.975,
                    "vol_ratio": 3.2,
                    "candle": {"open": 1.0, "close": 0.975},
                }
            },
            "market": {"agg_trade_buy_ratio_60s": 0.30},
        },
    )
    if dump_hit is None or dump_hit.direction != "short":
        issues.append("detect_cex_dump triple-gate fixture must fire short")

    import inspect
    from hunt_core.scanner.gate._policy_decl import _decl_check_ev_delivery as _ev_decl
    from hunt_core.scanner.setups import catalog as setup_catalog

    src = inspect.getsource(_ev_decl)
    if "legacy_fuel_delivery_enabled" not in src:
        issues.append("_decl_check_ev_delivery must retain legacy fuel escape hatch")
    from hunt_core.scanner.gate._policy_decl import _decl_check_playbook

    if not callable(_decl_check_playbook):
        issues.append("_decl_check_playbook must exist for declarative delivery")
    cat_src = inspect.getsource(setup_catalog)
    if "def merge_dump_initiation_into_setup" in cat_src:
        issues.append("legacy merge_dump_initiation_into_setup must be removed")
    if "def merge_catalog_long_into_setup" in cat_src:
        issues.append("legacy merge_catalog_long_into_setup must be removed")

    from hunt_core._dev.replay_row import batch_delivery_replay, recompute_tick_row

    replay_row = {
        "symbol": "TESTUSDT",
        "price": 1.0,
        "ts": "2026-06-18T12:00:00Z",
        "lifecycle": {"phase": "distribution", "recommended_bias": "short"},
        "market": {},
        "timeframes": {
            "15m_closed": {"closed_bar": True, "close": 1.0, "rsi14": 68, "atr14": 0.02},
            "5m_closed": {"closed_bar": True, "close": 0.99, "bearish": True},
        },
        "dump": {
            "dump_score": 55,
            "dump_fuel": 55,
            "entry_zone": [0.99, 1.01],
            "stop_loss": 1.05,
            "tp1": 0.95,
            "risk_reward": 1.8,
            "confirmed": True,
            "delivery_p_win": 0.52,
        },
        "long": {"long_score": 20, "long_fuel": 20},
    }
    replay_summary = batch_delivery_replay([replay_row], direction="short", recompute=False)
    if replay_summary.get("n") != 1:
        issues.append(f"batch_delivery_replay expected n=1 got {replay_summary.get('n')}")
    rep = (replay_summary.get("reports") or [{}])[0]
    if rep.get("p_win") != 0.52:
        issues.append(f"replay report must expose p_win got {rep.get('p_win')}")
    if rep.get("conviction") != 52.0:
        issues.append(f"replay report conviction expected 52 got {rep.get('conviction')}")

    recomputed = recompute_tick_row(dict(replay_row))
    if recomputed.get("recompute_note") != "jsonl_hydrate":
        issues.append("recompute_tick_row must hydrate JSONL lifecycle/mtf")
    if recomputed.get("lifecycle", {}).get("phase_fusion") != "distribution":
        issues.append("recompute_tick_row must backfill phase_fusion from phase")

    funnel_src = inspect.getsource(
        __import__(
            "hunt_core.deliver.dispatch",
            fromlist=["_record_delivery_funnel"],
        )._record_delivery_funnel
    )
    if 'setup.get("dump_fuel") or setup.get("long_fuel")' in funnel_src:
        issues.append("_record_delivery_funnel must not use cross-direction fuel fallback")

    from hunt_core.scanner.gate._delivery_helpers import closed_bar_candle

    live_wick = closed_bar_candle(
        {"1m_closed": {"closed_bar": False, "candle": {"bearish": True, "upper_wick_ratio": 0.9}}},
        "1m",
    )
    if live_wick:
        issues.append("T5: forming 1m bar must not contribute wick candle")
    closed_wick = closed_bar_candle(
        {"1m_closed": {"closed_bar": True, "candle": {"bearish": True, "upper_wick_ratio": 0.5}}},
        "1m",
    )
    if not closed_wick.get("bearish"):
        issues.append("T5: closed 1m bar must expose candle for wick scoring")

    from hunt_core.scanner.setups.catalog import legacy_fuel_merge_enabled
    from hunt_core.params.store import universal_section

    dl = universal_section("delivery")
    if bool(dl.get("ev_primary_default", True)) and legacy_fuel_merge_enabled():
        issues.append("ev_primary_default=true must keep legacy fuel merge off")

    from hunt_core.scanner.gate._ev import EV_PRIMARY_LEGACY_BLOCKERS
    from hunt_core.scanner.gate.delivery import collect_report_blockers

    ev_row = {
        "symbol": "VELVETUSDT",
        "price": 0.12,
        "session": {"pos_in_range": 0.08},
        "timeframes": {"5m_closed": {"closed_bar": True, "close": 0.12}},
    }
    ev_setup = {
        "ev_primary": True,
        "setup_id": "liquidity_sweep",
        "setup_type": "sweep_reclaim",
        "catalog_setup": "liquidity_sweep",
        "confirmed": True,
        "p_win": 0.76,
        "delivery_p_win": 0.76,
        "ev_primary_ev": 0.85,
        "entry_zone": [0.121, 0.122],
        "stop_loss": 0.125,
        "tp1": 0.115,
        "tp2": 0.110,
        "risk_reward": 2.0,
        "levels_viable": True,
    }
    ev_lc = {
        "phase": "dump_active",
        "recommended_bias": "wait",
        "fall_from_high_pct": 75.0,
        "short_entry_ok": False,
    }
    ev_codes = {
        b.code
        for b in collect_report_blockers(
            ev_setup,
            direction="short",
            symbol="VELVETUSDT",
            lifecycle=ev_lc,
            row=ev_row,
        )
    }
    leaked = EV_PRIMARY_LEGACY_BLOCKERS & ev_codes
    if leaked:
        issues.append(f"EV-primary must drop legacy phase/fuel blockers, got {sorted(leaked)}")

    mission_codes = {
        b.code
        for b in collect_report_blockers(
            ev_setup,
            direction="short",
            symbol="VELVETUSDT",
            lifecycle=ev_lc,
            row=ev_row,
        )
    }
    if "mission_mid_dump" not in mission_codes:
        issues.append("dump_active short must block with mission_mid_dump")

    from hunt_core.scanner.gate._mission import mission_delivery_block

    mid_pump = mission_delivery_block(
        direction="long",
        lifecycle={"phase": "impulse_initiating", "leg_gain_pct": 22.0},
        setup={"phase": "long_confirmed"},
        symbol="BEATUSDT",
    )
    if mid_pump is None or mid_pump.code != "mission_mid_pump":
        issues.append("impulse_initiating long must block with mission_mid_pump")

    from hunt_core.scanner.setups.catalog import promote_catalog_ev_setup

    pre_lake = promote_catalog_ev_setup(
        {"dump_score": 10.0},
        "short",
        {
            "setup_id": "bos_choch",
            "direction": "short",
            "strength": 0.72,
            "confirmed": True,
            "p_win": 0.58,
            "ev": 0.12,
            "reasons": ("bos_break",),
            "levels": {
                "entry_zone": [1.0, 1.01],
                "stop_loss": 1.05,
                "tp1": 0.95,
                "tp2": 0.90,
            },
            "lake_stats": {"by_setup": {"bos_choch:short": {"n": 2}}},
        },
    )
    if not pre_lake.get("ev_primary"):
        issues.append("promote_catalog_ev_setup must stamp ev_primary before lake flip (n<8)")
    if pre_lake.get("setup_type") != "bos_retest":
        issues.append("EV-primary bos_choch must map to bos_retest setup_type")

    from hunt_core.runtime import tick_io

    if tick_io.SENT_MESSAGES not in tick_io._TELEMETRY_JSONL:
        issues.append("sent_messages.jsonl must be in rotate_telemetry_jsonl (#44)")

    from hunt_core.deliver.templates import _squeeze_direction

    sq_row = {
        "squeeze": {},
        "lifecycle": {"phase": "distribution", "recommended_bias": "short"},
        "structure": {"structure_bias": "short"},
        "dump": {"delivery_p_win": 0.55},
        "long": {"delivery_p_win": 0.35},
    }
    emoji, _label, ev = _squeeze_direction(sq_row, phase_human=lambda p: p)
    if emoji != "🔴" or not any("Structure" in e for e in ev):
        issues.append("squeeze direction must prefer structure_bias + conviction over raw score")

    eval_src = inspect.getsource(
        __import__(
            "hunt_core.deliver.dispatch",
            fromlist=["evaluate_delivery"],
        ).evaluate_delivery
    )
    if 'setup.get("dump_score") or setup.get("long_score")' in eval_src:
        issues.append("evaluate_delivery must not use cross-direction score for family_vote")

    from hunt_core.deep.signal import resolve_trade_direction
    from hunt_core.data.universe import is_pinned_symbol as _pin_shim  # noqa: F401

    dir_row = {
        "symbol": "TESTUSDT",
        "lifecycle": {"recommended_bias": "wait", "phase": "distribution"},
        "dump": {"delivery_p_win": 0.35, "dump_fuel": 80},
        "long": {"delivery_p_win": 0.60, "long_fuel": 30},
    }
    picked, _, strength, _notes = resolve_trade_direction(dir_row)
    if picked != "long":
        issues.append(f"resolve_trade_direction must pick higher P(win) got {picked}")
    if strength < 55:
        issues.append(f"resolve_trade_direction strength expected ~60 got {strength}")

    struct_row = {
        "symbol": "TESTUSDT",
        "lifecycle": {"recommended_bias": "wait", "phase": "no_setup"},
        "structure": {"structure_bias": "short"},
        "dump": {"dump_fuel": 20},
        "long": {"long_fuel": 20},
    }
    struct_pick, _, _, struct_notes = resolve_trade_direction(struct_row)
    if struct_pick != "short":
        issues.append("resolve_trade_direction must honor structure_bias on wait")
    if not any("structure bias" in n for n in struct_notes):
        issues.append("resolve_trade_direction must note structure bias")

    from hunt_core.scanner.gate import _rules_table as rules_mod

    rule_ids = [r.id for r in rules_mod.DELIVERY_GATE_RULES]
    if "meme_pump_volume" not in rule_ids:
        issues.append("DELIVERY_GATE_RULES must include meme_pump_volume (A4)")

    from hunt_core.scanner.setups.catalog import HUNT_SETUP_IDS

    if "btc_decoupled" not in HUNT_SETUP_IDS:
        issues.append("HUNT_SETUP_IDS must include btc_decoupled (C1)")

    from hunt_core.track import tracker as trk_mod

    latch_src = inspect.getsource(trk_mod._latched_levels_payload)
    if "delivered_levels_snapshot" not in latch_src:
        issues.append("_latched_levels_payload must consume delivered_levels_snapshot")

    from hunt_core.runtime.cycle._cycle_confirm import _advisory_tg_enabled

    _prev_adv = os.environ.pop("HUNT_ADVISORY_TG", None)
    try:
        if _advisory_tg_enabled():
            issues.append("advisory TG must stay off by default after legacy purge")
    finally:
        if _prev_adv is not None:
            os.environ["HUNT_ADVISORY_TG"] = _prev_adv

    # dump_hunt / early advisory paths removed — fusion confirm-only on production lane.
    from hunt_core.scanner.gate._lifecycle_gates import collect_lifecycle_blockers

    stale_lifecycle = collect_lifecycle_blockers(
        {"confirmed": True, "entry_zone": [1.0, 1.01], "stop_loss": 1.05, "tp1": 0.95},
        direction="short",
        lifecycle={"phase": "distribution", "short_entry_ok": True},
        row={"price_stale": True, "symbol": "TESTUSDT"},
        symbol="TESTUSDT",
    )
    if not any(b.code == "price_stale" for b in stale_lifecycle):
        issues.append("collect_lifecycle_blockers must include price_stale")

    from hunt_core.market.live_price import resolve_live_price

    class _StaleFeed:
        def live_ticker(self, symbol: str, *, max_age_s: float | None = None):
            if max_age_s is not None:
                return None
            return {"last": 99.0, "ts_ms": 0}

        def live_bbo(self, symbol: str):
            return None

        def live_funding(self, symbol: str):
            return None

        def snapshot(self, symbol: str):
            return None

    px, src = resolve_live_price("TESTUSDT", ws_feed=_StaleFeed(), fallback=100.0)
    if px != 100.0 or src != "stale_ticker":
        issues.append(f"stale ws ticker must fall back to kline got px={px} src={src}")

    # Facade re-export smoke (post-ruff __all__ guard)
    from hunt_core.deliver import telegram as tg_facade
    from hunt_core.scanner.gate import delivery as gate_facade
    from hunt_core.runtime.cycle._impl import SYMBOL_TICK_TIMEOUT_S
    from hunt_core.track import tracker as tracker_facade

    for name in (
        "format_setup_lines",
        "format_signal_brief_telegram",
        "squeeze_trade_direction",
    ):
        if not callable(getattr(tg_facade, name, None)):
            issues.append(f"telegram facade missing {name}")
    for name in (
        "_effective_min_rr",
        "_snapshot_tier_from_row",
    ):
        if not callable(getattr(gate_facade, name, None)):
            issues.append(f"gate.delivery facade missing {name}")
    from hunt_core.scanner.gate._policy_decl import _decl_check_playbook as _playbook_fn

    if not callable(_playbook_fn):
        issues.append("gate.policy missing _decl_check_playbook")
    for name in ("evaluate_followups", "global_confirm_burst_cap_reached"):
        if not callable(getattr(tracker_facade, name, None)):
            issues.append(f"tracker facade missing {name}")
    if SYMBOL_TICK_TIMEOUT_S <= 0:
        issues.append("SYMBOL_TICK_TIMEOUT_S must be positive")

    # Manipulation fusion + forecasts (Pass A)
    from hunt_core.analysis.manipulation_fusion import evaluate_manipulation_fusion, squeeze_blocks_predump_short
    from hunt_core.maps.forecast import build_dump_forecast, build_ignition_forecast, build_maps_forecast
    from hunt_core.maps.oi import classify_oi_regime
    from hunt_core.analysis.playbook_eval import playbook_passes
    from hunt_core.deep.build import build_deep_analysis
    from hunt_core.scanner.gate._ev import pwin_gate_enabled

    if classify_oi_regime(20.0, 8.0) != "new_money_long":
        issues.append("oi regime new_money_long expected")
    if classify_oi_regime(20.0, -8.0) != "new_money_short":
        issues.append("oi regime new_money_short expected")

    dist_row = {
        "price": 100.0,
        "lifecycle": {"phase": "distribution", "leg_gain_pct": 80.0},
        "session": {"pos_in_range": 0.92},
        "market": {
            "map_cvd_divergence": "bearish_div",
            "funding_rate": 0.0001,
            "oi_change_pct": 18.0,
            "price_change_pct": -6.0,
            "liq_heatmap_nearest_long": 92.0,
            "map_vp_va_contraction": 0.8,
        },
        "structure": {"support_break": True},
        "maps": {"liquidation": {"forward_zones": [{"price_center": 90.0}]}},
    }
    fusion = evaluate_manipulation_fusion(dist_row)
    if fusion.archetype != "predump_short":
        issues.append(f"distribution fixture expected predump_short got {fusion.archetype}")
    from hunt_core.analysis.playbook_checks import playbook_pass_ratio

    if fusion.required_n > 0 and fusion.primary_score != playbook_pass_ratio(
        fusion.archetype, fusion.checks
    ):
        issues.append("primary_score must equal playbook pass ratio")
    from hunt_core.analysis.manipulation_fusion import assessment_to_dict

    fusion_dict = assessment_to_dict(fusion)
    if not fusion_dict.get("check_sources"):
        issues.append("fusion must expose check_sources for telemetry")
    if not playbook_passes("predump_short", fusion_dict.get("checks") or {}):
        issues.append("predump fixture must pass playbook checklist")
    dump_fc = build_dump_forecast(dist_row)
    if dump_fc is None or float(dump_fc["target_primary"]) >= 100.0:
        issues.append("build_dump_forecast must target below price")

    coil_row = {
        "price": 50.0,
        "lifecycle": {"phase": "accumulation"},
        "market": {
            "map_vp_accumulation": 0.62,
            "map_accum_bid_absorption": True,
            "map_cvd_divergence": "bullish_div",
            "map_vp_va_contraction": 0.75,
            "liq_heatmap_nearest_short": 55.0,
        },
        "maps": {"volume_profile": {"profiles": [{"hvn_nodes": [{"price": 54.0}]}]}},
    }
    coil_fc = build_maps_forecast(coil_row)
    if coil_fc is None or float(coil_fc["target_primary"]) <= 50.0:
        issues.append("coil forecast must target above price")

    sq_row = {
        "price": 10.0,
        "lifecycle": {"phase": "post_dump_bounce"},
        "market": {
            "funding_rate": -0.0003,
            "map_cvd_divergence": "bullish_div",
            "liq_heatmap_nearest_short": 10.8,
            "orderbook_imbalance": 0.12,
        },
        "session": {"change_24h_pct": -25.0},
    }
    sq_fc = build_ignition_forecast(sq_row)
    if sq_fc is None or float(sq_fc["target_primary"]) <= 10.0:
        issues.append("ignition forecast must target above price")

    squeeze_row = {
        "price": 1.0,
        "market": {
            "funding_rate": -0.001,
            "taker_buy_sell_ratio": 1.05,
            "map_accum_bid_absorption": True,
            "map_cvd_divergence": "bullish_div",
        },
    }
    if not squeeze_blocks_predump_short(squeeze_row):
        issues.append("squeeze fixture must block predump")

    deep = build_deep_analysis(dist_row, full=False)
    if not isinstance(deep.forecasts, dict):
        issues.append("deep report missing forecasts")

    if pwin_gate_enabled():
        issues.append("pwin_gate should default off")

    low_p_setup = {
        "confirmed": True,
        "entry_zone": [1.0, 1.01],
        "stop_loss": 1.05,
        "tp1": 0.95,
        "delivery_ev": 0.02,
        "delivery_p_win": 0.30,
    }
    low_blocked = _decl_check_ev_delivery(
        row={"symbol": "TESTUSDT", "market": {}, "structure": {}},
        setup=dict(low_p_setup),
        direction="short",
        lifecycle={"phase": "distribution"},
        delivery_tier="triggered",
        symbol="TESTUSDT",
    )
    if low_blocked is not None:
        issues.append("low p_win must not block EV delivery when pwin_gate off")

    from hunt_core.scanner.gate._policy_decl import _decl_check_playbook as _playbook_check

    weak_row = dict(dist_row)
    weak_row["manipulation_fusion"] = assessment_to_dict(fusion)
    weak_checks = dict((weak_row["manipulation_fusion"] or {}).get("checks") or {})
    weak_checks["distribution_phase"] = False
    weak_checks["bear_cvd_div"] = False
    weak_checks["sweep_reclaim"] = False
    weak_row["manipulation_fusion"]["checks"] = weak_checks
    pb_blocked = _playbook_check(
        row=weak_row,
        setup={"confirmed": True},
        direction="short",
        lifecycle={"phase": "distribution"},
        delivery_tier="triggered",
        symbol="TESTUSDT",
    )
    if pb_blocked is None or pb_blocked.code != "playbook_fail":
        issues.append("playbook fail must block delivery")

    from hunt_core.analysis.manipulation_fusion import evaluate_manipulation_fusion, assessment_to_dict
    from hunt_core.scanner.gate._delivery_helpers import cluster_fuel

    if cluster_fuel(["5m_rejection"], raw_score=80.0, symbol="TESTUSDT") != 80.0:
        issues.append("cluster_fuel must passthrough raw_score when legacy fuel off")

    pwin_setup = {
        **armed_setup,
        "delivery_p_win": 0.62,
        "dump_fuel": 40,
    }
    pwin_card = format_delivery_card(
        armed_row, direction="short", setup=pwin_setup, delivery_tier="triggered"
    )
    if "conviction <code>62</code>" not in pwin_card:
        issues.append("card conviction must come from delivery_p_win")
    for bad in ("veto_levels:", "must_pass:", "Fast lane shadow"):
        if bad in pwin_card:
            issues.append(f"card must not leak internal code {bad!r}")

    from hunt_core.scanner.gate._mission import (
        hunt_skip_reason,
        is_mid_leg_phase,
        is_watch_hunt_phase,
    )

    if not is_mid_leg_phase("dump_active"):
        issues.append("is_mid_leg_phase must include dump_active")
    if not is_mid_leg_phase("mid"):
        issues.append("is_mid_leg_phase must include fusion mid")
    if not is_watch_hunt_phase("distribution", "short"):
        issues.append("distribution must be watch-hunt short phase")
    if hunt_skip_reason("dump_active", "short") != "mid_leg":
        issues.append("hunt_skip_reason dump_active must be mid_leg")
    if hunt_skip_reason("distribution", "short") is not None:
        issues.append("distribution short must not be hunt-skipped")

    from hunt_core.scanner.detect.phase import PRE_DUMP, MID
    from hunt_core.scanner.gate._mission import mission_delivery_block
    from hunt_core.scanner.gate._lifecycle import fusion_lifecycle_flags, fusion_lifecycle_dict

    pre_dump_lc = fusion_lifecycle_dict(
        None,
        structure_bias="",
        fall_from_high_pct=3.0,
        leg_gain_pct=10.0,
    )
    pre_dump_lc.update(
        {
            "phase": PRE_DUMP,
            "phase_fusion": PRE_DUMP,
            **fusion_lifecycle_flags(
                side="short", phase=PRE_DUMP, gate_open=True, watch_ok=True
            ),
        }
    )
    mission_pre = mission_delivery_block(
        direction="short", lifecycle=pre_dump_lc, setup={"phase": PRE_DUMP, "confirmed": True}
    )
    if mission_pre is not None:
        issues.append(f"fusion pre_dump short must pass mission gate got {mission_pre.code}")

    mid_lc = {**pre_dump_lc, "phase": MID, "phase_fusion": MID}
    mission_mid = mission_delivery_block(direction="short", lifecycle=mid_lc, setup={})
    if mission_mid is None or mission_mid.code != "mission_mid_dump":
        issues.append("fusion mid short must block with mission_mid_dump")

    prep_row = {
        "market": {
            "oi_z": 1.1,
            "map_accumulation_score": 0.55,
            "depth_imbalance": 0.18,
            "map_absorption_count": 2,
            "map_cvd_divergence": "bullish_div",
            "funding_rate": -0.0002,
            "map_accum_bid_absorption": True,
            "map_poc_migration_1h": "up",
        }
    }
    mid_long_lc = {**pre_dump_lc, "phase": "mid", "phase_fusion": "mid", "leg_gain_pct": 4.0}
    mission_prep_mid = mission_delivery_block(
        direction="long",
        lifecycle=mid_long_lc,
        setup={"phase": "mid"},
        symbol="BELUSDT",
        row=prep_row,
    )
    if mission_prep_mid is not None:
        issues.append(
            f"prep_ready long must bypass mission_mid_pump got {mission_prep_mid.code}"
        )

    flags = fusion_lifecycle_flags(
        side="short", phase=PRE_DUMP, gate_open=False, watch_ok=True
    )
    if not flags.get("short_entry_ok"):
        issues.append("pre_dump watch_ok must set short_entry_ok=True")

    from hunt_core.market.cross import (
        funding_rest_poll_venues,
        funding_ws_venues,
        sanitize_funding_map,
    )

    if "bybit" not in funding_rest_poll_venues():
        issues.append("bybit must use REST funding poll plane")
    if "okx" not in funding_ws_venues():
        issues.append("okx must use WS funding plane")
    cleaned = sanitize_funding_map({"binance": 0.0001, "bybit": None, "okx": 0.0002})
    if None in cleaned.values() or "bybit" in cleaned:
        issues.append("sanitize_funding_map must drop null venue rates")
    if cleaned.get("binance") != 0.0001 or cleaned.get("okx") != 0.0002:
        issues.append("sanitize_funding_map must keep finite rates")

    from hunt_core.runtime.tick_jsonl import (
        ensure_fusion_lifecycle_fields,
        hydrate_tick_row_from_jsonl,
        prepare_tick_row_for_jsonl,
    )

    lc_fix = ensure_fusion_lifecycle_fields(
        {"phase": "pre_pump", "watch_ok": True},
        setup={"direction": "long", "confirmed": True, "phase": "pre_pump"},
    )
    if lc_fix.get("phase_fusion") != "pre_pump" or lc_fix.get("long_entry_ok") is not True:
        issues.append("ensure_fusion_lifecycle_fields must backfill phase_fusion + long_entry_ok")

    raw_row = {
        "symbol": "BTCUSDT",
        "price": 100.0,
        "lifecycle": {"phase": "pre_pump"},
        "long": {"confirmed": True, "direction": "long", "phase": "pre_pump", "fusion_score": 98.9},
        "mtf": "MTFConfluence(symbol='BTCUSDT')",
        "timeframes": {"4h": {"close": 100, "ema20": 99, "ema50": 98, "rsi14": 55, "adx14": 22}},
    }
    hydrated = hydrate_tick_row_from_jsonl(raw_row)
    if isinstance(hydrated.get("mtf"), str):
        issues.append("hydrate must drop corrupted string mtf")
    prepared = prepare_tick_row_for_jsonl(hydrated)
    if prepared.get("lifecycle", {}).get("phase_fusion") is None:
        issues.append("prepare_tick_row_for_jsonl must emit phase_fusion")
    if isinstance(prepared.get("mtf"), str):
        issues.append("prepare_tick_row_for_jsonl must not emit string mtf")

    from hunt_core.features.prepare_columns import should_bypass_kline_integrity

    if not should_bypass_kline_integrity(bars_4h=12, bars_1h=48, bars_15m=195):
        issues.append("should_bypass_kline_integrity must allow REUSDT-class listings")
    from hunt_core.features.prepare_columns import violations_are_partial_history_only

    slx_v = [
        "klines.1h.rows=464<min_raw=498",
        "klines.4h.rows=116<min_raw=498",
    ]
    if not violations_are_partial_history_only(slx_v):
        issues.append("violations_are_partial_history_only must allow SLXUSDT-class gaps")
    stale_v = ["klines.15m.stale.SYNUSDT.2341796ms>2250000ms"]
    if violations_are_partial_history_only(stale_v):
        issues.append("violations_are_partial_history_only must reject stale violations")

    from hunt_core.scanner.gate.policy import EdgePolicyConfig, long_tg_allowed

    ok_long, long_reason = long_tg_allowed(
        EdgePolicyConfig(wide_hunter=False, long_tg_enabled=True)
    )
    if not ok_long or long_reason != "env_override":
        issues.append("long_tg_allowed must pass with HUNT_LONG_TG without wide_hunter")

    from hunt_core.scanner.gate._registry import _gate_edge_policy

    _prev_long_tg = os.environ.pop("HUNT_LONG_TG", None)
    ramp_setup: dict[str, Any] = {}
    _gate_edge_policy(direction="long", setup=ramp_setup, row={}, lifecycle=None)
    if _prev_long_tg is not None:
        os.environ["HUNT_LONG_TG"] = _prev_long_tg
    if ramp_setup.get("delivery_lane") != "lab" or not ramp_setup.get("long_ramp_reason"):
        issues.append("uncalibrated long must route to lab lane via edge_policy")

    from hunt_core.scanner.detect.delivery_support import liquidity_skip_reason
    from hunt_core.scanner.gate._quality import (
        _row_chg24_abs,
        meme_anomaly_block_code,
        passes_meme_anomaly_gate,
    )

    if liquidity_skip_reason(quote_volume="bad", oi=1.0, last_price=1.0) != "liquidity_quote_vol_invalid":
        issues.append("liquidity_skip_reason must reject invalid quote_volume")
    if _row_chg24_abs({}) is not None:
        issues.append("_row_chg24_abs must return None when chg24 missing")
    _cal = type(
        "Cal",
        (),
        {"anomaly_min_chg_24h_pct": 5.0, "anomaly_min_range_24h_pct": 8.0},
    )()
    if passes_meme_anomaly_gate(sym="TESTUSDT", row={}, lc={}, cal=_cal):
        issues.append("passes_meme_anomaly_gate must fail when chg24 and range missing")
    if meme_anomaly_block_code(sym="TESTUSDT", row={}, lc={}, cal=_cal) != "data.chg24_missing":
        issues.append("meme_anomaly_block_code must return data.chg24_missing when inputs absent")

    from hunt_core.deliver.dispatch import unified_cooldown_ok

    now = datetime.now(UTC)
    if unified_cooldown_ok(
        {"unified:TESTUSDT:short:confirm": "not-an-iso-timestamp"},
        symbol="TESTUSDT",
        direction="short",
        stage="confirm",
        now=now,
    ):
        issues.append("unified_cooldown_ok must fail-closed on corrupt confirm timestamp")

    from hunt_core.deliver.dispatch import readiness_score

    if readiness_score({}, direction="short") is not None:
        issues.append("readiness_score must return None when dump_score/fusion absent")

    from hunt_core.scanner.gate._policy_decl import _decl_check_ev_delivery

    ev_gate = _decl_check_ev_delivery(
        row={"symbol": "TESTUSDT", "market": {}},
        setup={"confirmed": False},
        direction="short",
        lifecycle={},
        delivery_tier="forming",
        symbol="TESTUSDT",
    )
    if ev_gate is None or getattr(ev_gate, "code", None) != "data.ev_missing":
        issues.append("_decl_check_ev_delivery must gate data.ev_missing when EV and P(win) absent")

    from hunt_core.deep.format_pinned_signal import (
        _gate_diagnostic_lines,
        _hypothesis_header,
        _show_activation_block,
        _use_soft_narrative,
    )

    if _show_activation_block("WAIT", "poor"):
        issues.append("_show_activation_block must hide activation on WAIT+poor")
    if not _use_soft_narrative("WAIT", "moderate", 0.6):
        issues.append("_use_soft_narrative must soften labels on WAIT")
    if _hypothesis_header("WAIT", ["strength", "rr_primary"]) != "Гипотеза (отклонена)":
        issues.append("_hypothesis_header must mark rejected hypothesis on strength/RR fail")
    diag = _gate_diagnostic_lines(
        ["strength", "rr_primary"],
        ("стакан против шорта",),
        strength_score=0.24,
        strength_min=0.5,
        fragility_score=0.4,
        fragility_max=0.65,
        plan=type("P", (), {"rr_primary": 0.42})(),
        rr_min=0.75,
    )
    if len(diag) < 3 or "0.24" not in diag[0]:
        issues.append("_gate_diagnostic_lines must expose numeric gate diagnostics")

    if issues:
        for item in issues:
            print(f"FAIL {item}", file=sys.stderr)
        return 1
    print("check_logic ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

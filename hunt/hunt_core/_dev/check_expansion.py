"""Expansion Engine truth tests — block behaviour, score separation, isolation.

Synthetic rows only (no network). Mirrors the plan's verification cases:
  A  high compression + fuel, activation far  → expansion high, trigger low
  B  same + activation near                    → both high
  C  ambiguous P(up)≈P(down)                    → state not forced directional
  D  rising history                            → positive delta momentum
  Tier C  vacuum / breakout failure / spring vs upthrust
  Strict MTF  1D distribution + 15m compression → fractal htf conflict
  OpportunityScore ≠ expansion_score ordering
  Import isolation  verdict_v2 ↔ expansion_engine zero cross-imports
"""
from __future__ import annotations

import sys
from pathlib import Path

from hunt_core.analysis.expansion_engine.blocks import (
    breakout_failure,
    fractal_alignment,
    liquidity_vacuum,
    wyckoff_signals,
)
from hunt_core.analysis.expansion_engine.expansion.orchestrator import build_expansion_opportunity
from hunt_core.analysis.expansion_engine.history import global_history
from hunt_core.analysis.expansion_engine.ranking.opportunity_score import compute_opportunity_score
from hunt_core.analysis.expansion_engine.state_machine import global_state_machine
from hunt_core.analysis.expansion_engine.types import BlockContext


def _reset() -> None:
    global_history().clear()
    global_state_machine().clear()


def _pre_pump_row(*, activation_pct: float) -> dict:
    price = 1.0
    void = price * (1.0 + activation_pct / 100.0)
    near_above = price * (1.0 + activation_pct / 100.0)
    return {
        "symbol": "TESTUSDT",
        "price": price,
        "chg_24h_pct": 0.4,
        "market": {
            "map_accumulation_score": 0.7,
            "map_accum_bid_absorption": True,
            "map_cvd_divergence": "bullish_div",
            "oi_z": 2.3,
            "oi_chg_1h": 7.0,
            "funding_pct": -0.012,
            "funding_zscore_48h": -1.8,
            "liq_squeeze_fuel_short": 0.8,
            "map_void_above": void,
            "map_void_above_pct": activation_pct,
            "map_ask_thinning": True,
            "liq_heatmap_nearest_short": near_above,
            "agg_trade_delta": 0.42,
            "vol_ratio": 0.85,
        },
        "regime": {"market_regime": "expansion", "btc_decoupled_pump": True},
        "structure": {
            "htf_trend": "bull",
            "bos_direction": "bull",
            "choch_detected": True,
            "at_level": True,
            "structure_bias": "long",
            "liquidity_pools": {
                "equal_highs": [{"price": near_above, "count": 2}],
                "equal_lows": [{"price": 0.9, "count": 2}],
                "nearest_above": near_above,
                "nearest_below": 0.9,
            },
        },
        "timeframes": {
            "1d": {"rsi14": 34, "bb_width_pctile": 0.25, "atr_pct": 3.0, "macd_hist": 0.001},
            "4h": {"rsi14": 45, "bb_width_pctile": 0.10, "squeeze_on": True, "macd_hist": 0.001},
            "1h": {"rsi14": 52, "bb_width_pctile": 0.08, "squeeze_on": True, "atr14": 0.01, "macd_hist": 0.001, "vol_ratio": 0.8, "prev_high": 1.05},
            "15m": {"rsi14": 55, "bb_width_pctile": 0.12, "macd_hist": 0.002, "adx14": 26},
        },
        "btc_context": {"chg_24h_pct": 0.2, "regime": "range"},
    }


def _fail(msg: str) -> int:
    print(f"FAIL {msg}", file=sys.stderr)
    return 1


def check_trigger_vs_score() -> int:
    _reset()
    far = build_expansion_opportunity(_pre_pump_row(activation_pct=18.0))
    _reset()
    near = build_expansion_opportunity(_pre_pump_row(activation_pct=1.0))
    if far.expansion_score < 0.6:
        return _fail(f"A expected high expansion_score, got {far.expansion_score}")
    if far.trigger_probability >= near.trigger_probability:
        return _fail(
            f"A/B trigger should rise as activation nears: far={far.trigger_probability} near={near.trigger_probability}"
        )
    if near.trigger_probability < 0.55:
        return _fail(f"B expected high trigger with near activation, got {near.trigger_probability}")
    print(f"  A/B ok: score={far.expansion_score} trig_far={far.trigger_probability} trig_near={near.trigger_probability}")
    return 0


def check_ambiguous_pivot() -> int:
    _reset()
    row = {
        "symbol": "AMBUSDT",
        "price": 1.0,
        "chg_24h_pct": 3.0,
        "market": {
            "map_accumulation_score": 0.5,
            "map_accum_bid_absorption": True,
            "oi_chg_1h": 5.0,
            "oi_z": 1.5,
            "funding_pct": 0.03,  # crowded long -> down evidence
            "agg_trade_delta": 0.4,  # sell into rally -> distribution
            "vol_ratio": 0.8,
            "liq_squeeze_fuel_long": 0.6,
        },
        "structure": {"htf_trend": "neutral", "structure_bias": "wait"},
        "timeframes": {"1h": {"rsi14": 55, "bb_width_pctile": 0.3}},
    }
    opp = build_expansion_opportunity(row)
    if opp.state in {"pre_pump", "pre_dump", "active_pump", "active_dump"}:
        return _fail(f"C ambiguous should not force directional state, got {opp.state}")
    print(f"  C ok: state={opp.state} p={opp.probabilities.to_dict()}")
    return 0


def check_delta_momentum() -> int:
    _reset()
    row = _pre_pump_row(activation_pct=3.0)
    sym = "MOMUSDT"
    row["symbol"] = sym
    # Seed a weak past so the current strong read registers as rising.
    global_history().record(sym, {k: 0.1 for k in (
        "compression", "fuel", "funding", "liquidity", "structure",
        "fuel_imbalance", "supply_exhaustion",
    )})
    opp = build_expansion_opportunity(row)
    if opp.deltas.momentum <= 0.5:
        return _fail(f"D expected positive momentum after rising history, got {opp.deltas.momentum}")
    print(f"  D ok: momentum={opp.deltas.momentum}")
    return 0


def check_tier_c_blocks() -> int:
    _reset()
    vac_ctx = BlockContext.from_row({
        "symbol": "VACUSDT", "price": 1.0,
        "market": {"map_void_above": 1.05, "map_void_above_pct": 5.0, "map_ask_thinning": True},
    })
    vac = liquidity_vacuum.score(vac_ctx)
    if not vac.active or vac.direction != "up" or vac.score <= 0:
        return _fail(f"vacuum expected active up, got {vac}")

    bf_ctx = BlockContext.from_row({
        "symbol": "BFUSDT", "price": 1.0,
        "market": {"vol_ratio": 0.6, "oi_chg_1h": -1.0},
        "timeframes": {"1h": {"prev_high": 1.0, "rsi14": 72, "vol_ratio": 0.6}},
    })
    bf = breakout_failure.score(bf_ctx)
    if not bf.active or bf.direction != "down":
        return _fail(f"breakout_failure expected active down, got {bf}")

    spring_ctx = BlockContext.from_row({
        "symbol": "SPRUSDT", "price": 1.0,
        "market": {"map_cvd_divergence": "bullish_div"},
        "structure": {"choch_detected": True, "at_level": True, "bos_direction": "bull"},
    })
    spring = wyckoff_signals.score_spring(spring_ctx)
    upthrust = wyckoff_signals.score_upthrust(spring_ctx)
    if not spring.active or spring.direction != "up":
        return _fail(f"spring expected active up, got {spring}")
    if upthrust.active:
        return _fail("upthrust should abstain on a bull-reclaim row")
    print(f"  Tier C ok: vacuum={vac.score:.2f} breakout_failure={bf.score:.2f} spring={spring.score:.2f}")
    return 0


def check_strict_mtf() -> int:
    ctx = BlockContext.from_row({
        "symbol": "MTFUSDT", "price": 1.0,
        "timeframes": {
            "1d": {"rsi14": 74, "macd_hist": -0.01},   # distribution / down
            "4h": {"bb_width_pctile": 0.1, "squeeze_on": True, "macd_hist": 0.001},
            "1h": {"bb_width_pctile": 0.08, "squeeze_on": True, "macd_hist": 0.001},
            "15m": {"bb_width_pctile": 0.1, "squeeze_on": True, "macd_hist": 0.001},
        },
    })
    res = fractal_alignment.score(ctx)
    if not res.active:
        return _fail("strict MTF expected active result")
    if "htf_conflict" not in res.evidence:
        return _fail(f"strict MTF expected htf_conflict penalty, evidence={res.evidence}")
    print(f"  strict MTF ok: score={res.score:.2f} evidence={res.evidence}")
    return 0


def check_opportunity_ordering() -> int:
    # "Ready but far / thin" should rank below "less ready but near + liquid".
    a = compute_opportunity_score(
        expansion_quality=0.9, trigger_probability=0.2,
        liquidity_score=0.2, cycle_score=0.2, fake_breakout_risk=0.1,
    )
    b = compute_opportunity_score(
        expansion_quality=0.6, trigger_probability=0.8,
        liquidity_score=0.8, cycle_score=0.8, fake_breakout_risk=0.1,
    )
    if not b > a:
        return _fail(f"OpportunityScore ordering wrong: a={a} b={b}")
    print(f"  opportunity ordering ok: a={a} b={b}")
    return 0


def _imports_referencing(py: Path, needle: str) -> bool:
    """True if any import statement in ``py`` references ``needle`` (ignores comments)."""
    import ast

    try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(needle in alias.name for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and needle in node.module:
                return True
    return False


def check_rotation_scan() -> int:
    """Universe scan folds cross-universe rotation into OpportunityScore."""
    _reset()
    from hunt_core.analysis.expansion_engine import rank_universe

    hot = _pre_pump_row(activation_pct=1.0)
    hot["symbol"] = "HOTUSDT"
    cold = _pre_pump_row(activation_pct=14.0)
    cold["symbol"] = "COLDUSDT"
    cold["market"]["oi_z"] = 0.6
    cold["market"]["oi_chg_1h"] = 1.0
    res = rank_universe([hot, cold], top_n=10)
    pump = res.get("pre_pump") or []
    if len(pump) < 2:
        return _fail(f"rotation scan expected 2 pre_pump, got {len(pump)}")
    if any(o.meta.sector_rotation is None for o in pump):
        return _fail("rotation scan should populate meta.sector_rotation")
    if pump[0].symbol != "HOTUSDT":
        return _fail(f"rotation scan expected HOTUSDT ranked first, got {pump[0].symbol}")
    print(f"  rotation scan ok: top={pump[0].symbol} rot={pump[0].meta.sector_rotation}")
    return 0


def check_from_dict_roundtrip() -> int:
    """Stamped expansion dict round-trips and scan fast-path matches."""
    _reset()
    from hunt_core.analysis.expansion_engine.expansion.orchestrator import (
        build_expansion_opportunity,
        opportunity_from_row,
    )
    from hunt_core.analysis.expansion_engine.types import ExpansionOpportunity

    row = _pre_pump_row(activation_pct=1.0)
    built = build_expansion_opportunity(row)
    payload = built.to_dict()
    restored = ExpansionOpportunity.from_dict(payload)
    if restored.symbol != built.symbol:
        return _fail("from_dict symbol mismatch")
    if abs(restored.expansion_score - built.expansion_score) > 1e-6:
        return _fail("from_dict score mismatch")

    row["expansion"] = payload
    fast = opportunity_from_row(row, prefer_stamped=True)
    if abs(fast.meta.opportunity_score - built.meta.opportunity_score) > 1e-6:
        return _fail("opportunity_from_row stamped path mismatch")
    print("  from_dict roundtrip ok")
    return 0


def check_history_persist() -> int:
    """Block-score history round-trips through runtime state file."""
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from hunt_core.analysis.expansion_engine.config import ExpansionConfig
    from hunt_core.analysis.expansion_engine.history import global_history
    from hunt_core.analysis.expansion_engine.runtime_state import (
        load_expansion_runtime_state,
        save_expansion_runtime_state,
    )

    global_history().clear()
    global_history().record("HISTUSDT", {"compression": 0.4, "fuel": 0.3})
    global_history().record("HISTUSDT", {"compression": 0.9, "fuel": 0.5})

    cfg = ExpansionConfig(
        enabled=True,
        history_persist=True,
        history_persist_samples=40,
        history_persist_max_symbols=64,
    )

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "expansion_runtime_state.json"
        with patch(
            "hunt_core.analysis.expansion_engine.runtime_state.EXPANSION_RUNTIME_STATE_JSON",
            state_path,
        ), patch(
            "hunt_core.analysis.expansion_engine.runtime_state.load_expansion_config",
            return_value=cfg,
        ), patch(
            "hunt_core.analysis.expansion_engine.runtime_state._persist_symbols",
            return_value={"HISTUSDT"},
        ):
            save_expansion_runtime_state()
            global_history().clear()
            load_expansion_runtime_state()
            latest = global_history().past_scores("HISTUSDT", lookback=0)
            if latest is None or latest.get("compression", 0) < 0.8:
                return _fail(f"history restore failed: {latest}")
            reloaded = json.loads(state_path.read_text(encoding="utf-8"))
            if "history" not in reloaded:
                return _fail("history block missing from runtime state")

    global_history().clear()
    print("  history persist ok")
    return 0


def check_fsm_persist() -> int:
    """FSM snapshot round-trips through JSON file."""
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from hunt_core.analysis.expansion_engine.runtime_state import (
        load_expansion_runtime_state,
        save_expansion_runtime_state,
    )
    from hunt_core.analysis.expansion_engine.state_machine import (
        ExpansionStateMachine,
        global_state_machine,
    )

    fsm = ExpansionStateMachine()
    fsm.transition("TESTUSDT", "pre_pump")
    fsm.transition("TESTUSDT", "pre_dump")
    snap = fsm.snapshot()
    if snap.get("TESTUSDT", {}).get("state") != "pre_pump":
        return _fail(f"expected hysteresis hold pre_pump, got {snap}")

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "expansion_runtime_state.json"
        state_path.write_text(json.dumps({"fsm": snap}), encoding="utf-8")
        with patch(
            "hunt_core.analysis.expansion_engine.runtime_state.EXPANSION_RUNTIME_STATE_JSON",
            state_path,
        ):
            global_state_machine().clear()
            load_expansion_runtime_state()
            restored = global_state_machine().snapshot()
            if restored.get("TESTUSDT") != snap.get("TESTUSDT"):
                return _fail(f"FSM restore mismatch {restored}")
            save_expansion_runtime_state()
            reloaded = json.loads(state_path.read_text(encoding="utf-8"))
            if reloaded.get("fsm", {}).get("TESTUSDT") != snap.get("TESTUSDT"):
                return _fail("save_expansion_runtime_state did not persist FSM")

    global_state_machine().clear()
    print("  fsm persist ok")
    return 0


def check_universe_scan() -> int:
    """Universe alert selection skips pinned and respects fingerprint dedup."""
    import tempfile
    from dataclasses import replace
    from pathlib import Path
    from unittest.mock import patch

    from hunt_core.analysis.expansion_engine.config import ExpansionConfig
    from hunt_core.analysis.expansion_engine.types import (
        BlockScores,
        BlockDeltas,
        ExpansionOpportunity,
        ExpansionProbabilities,
        MetaScores,
    )
    from hunt_core.runtime.expansion_alerts import (
        expansion_change_fingerprint,
        last_alert_fingerprint,
        mark_expansion_alert_sent,
    )
    from hunt_core.runtime.expansion_universe_scan import should_universe_alert

    cfg = ExpansionConfig(
        tg_universe_min_opp=0.35,
        tg_min_quality=0.45,
        tg_min_trigger=0.50,
        fake_breakout_block=0.55,
        tg_cooldown_min=0,
    )

    def _opp(sym: str, *, opp: float = 0.55, quality: float = 0.62) -> ExpansionOpportunity:
        return ExpansionOpportunity(
            symbol=sym,
            price=1.0,
            state="pre_pump",
            stage="compression",
            lifecycle_stage=4,
            probabilities=ExpansionProbabilities(p_up=0.65, p_down=0.12, p_none=0.23),
            expansion_score=0.6,
            trigger_probability=0.71,
            meta=MetaScores(
                expansion_quality=quality,
                fake_breakout_risk=0.2,
                opportunity_score=opp,
            ),
            blocks=BlockScores(liquidity=0.5, cycle_context=0.4),
            deltas=BlockDeltas(),
            main_drivers=("compression",),
            readiness="high",
            risk="medium",
            coverage=0.8,
        )

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "expansion_alert_state.json"
        with patch(
            "hunt_core.runtime.expansion_alerts.EXPANSION_ALERT_STATE",
            state_path,
        ):
            alt = _opp("ALTUSDT")
            if not should_universe_alert(alt, cfg):
                return _fail("ALT should universe-alert")
            if should_universe_alert(_opp("BTCUSDT"), cfg):
                return _fail("pinned BTC should be skipped")

            fp = expansion_change_fingerprint(alt.to_dict())
            mark_expansion_alert_sent("ALTUSDT", fingerprint=fp)
            if should_universe_alert(alt, cfg):
                return _fail("duplicate fingerprint should suppress alert")

            bumped = replace(alt, trigger_probability=0.82)
            if not should_universe_alert(bumped, cfg):
                return _fail("fingerprint change should re-alert")
            if last_alert_fingerprint("ALTUSDT") != fp:
                return _fail("fingerprint should persist in alert state")

    print("  universe scan ok")
    return 0


def check_expansion_alerts() -> int:
    """Pinned expansion TG policy — eligibility, fingerprint, change detection."""
    from datetime import UTC, datetime

    from hunt_core.analysis.expansion_engine.config import ExpansionConfig
    from hunt_core.runtime.expansion_alerts import (
        expansion_alert_eligible,
        expansion_change_fingerprint,
        material_expansion_change,
    )

    cfg = ExpansionConfig(
        tg_pinned_alerts=True,
        tg_on_change=True,
        tg_min_quality=0.45,
        tg_min_trigger=0.50,
        fake_breakout_block=0.55,
    )

    def _exp(
        *,
        state: str = "pre_pump",
        quality: float = 0.62,
        trigger: float = 0.71,
        dominant: str = "up",
    ) -> dict:
        return {
            "state": state,
            "dominant": dominant,
            "lifecycle_stage": 4,
            "trigger_probability": trigger,
            "meta": {
                "expansion_quality": quality,
                "opportunity_score": 0.55,
                "fake_breakout_risk": 0.2,
            },
            "probabilities": {"p_up": 0.65, "p_down": 0.12, "p_none": 0.23},
        }

    low = _exp(state="neutral", quality=0.3, trigger=0.2, dominant="neutral")
    if expansion_alert_eligible(low, cfg):
        return _fail("neutral low-quality should not be eligible")

    high = _exp()
    if not expansion_alert_eligible(high, cfg):
        return _fail("pre_pump with quality should be eligible")

    now = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    row_a = {"symbol": "TESTUSDT", "ts": now.isoformat(), "expansion": high}
    if not material_expansion_change("TESTUSDT", row_a, prev=None, cfg=cfg, now=now):
        return _fail("first eligible tick should alert")

    row_b = {
        "symbol": "TESTUSDT",
        "ts": now.isoformat(),
        "expansion": _exp(trigger=0.72),
    }
    if expansion_change_fingerprint(row_a["expansion"]) == expansion_change_fingerprint(row_b["expansion"]):
        return _fail("trigger change should alter fingerprint")
    if not material_expansion_change("TESTUSDT", row_b, prev=row_a, cfg=cfg, now=now):
        return _fail("material trigger shift should alert")

    row_same = dict(row_a)
    if material_expansion_change("TESTUSDT", row_same, prev=row_a, cfg=cfg, now=now):
        return _fail("identical fingerprint should not alert (within stale window)")

    print("  expansion alerts ok")
    return 0


def check_format_helpers() -> int:
    """Telegram format helpers render without error."""
    from hunt_core.analysis.expansion_engine.format import (
        format_calibration_report,
        format_outcome_stats,
        format_review_summary,
    )

    stats = format_outcome_stats({"signals": 3, "graded": 0}, pending_reviews=2, records=3)
    if "Expansion Outcomes" not in stats:
        return _fail("format_outcome_stats broken")
    cal = format_calibration_report({"status": "insufficient_samples", "samples": 5})
    if "insufficient" not in cal:
        return _fail("format_calibration_report broken")
    rev = format_review_summary({"graded": 2, "calibration": "refreshed"})
    if "Graded" not in rev:
        return _fail("format_review_summary broken")
    print("  format helpers ok")
    return 0


def check_config_toml() -> int:
    """Config loads from [hunt.expansion] in config.defaults.toml."""
    from hunt_core.analysis.expansion_engine.config import (
        invalidate_expansion_config_cache,
        load_expansion_config,
    )

    invalidate_expansion_config_cache()
    cfg = load_expansion_config()
    if not cfg.enabled:
        return _fail("expected expansion enabled from defaults")
    if cfg.scan_top_n != 50:
        return _fail(f"expected scan_top_n=50, got {cfg.scan_top_n}")
    if cfg.review_interval_s <= 0:
        return _fail("review_interval_s must be positive")
    if not cfg.watch_stamp:
        return _fail("expected watch_stamp=true from defaults")
    if not cfg.history_persist:
        return _fail("expected history_persist=true from defaults")
    print(
        f"  config ok: scan_top_n={cfg.scan_top_n} review_s={cfg.review_interval_s} "
        f"watch_stamp={cfg.watch_stamp}"
    )
    return 0


def check_calibration_apply() -> int:
    """Persisted multipliers adjust weight tables on config load."""
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from hunt_core.analysis.expansion_engine.config import (
        _DEFAULT_UP_WEIGHTS,
        invalidate_expansion_config_cache,
        load_expansion_config,
    )

    payload = {
        "status": "ok",
        "samples": 25,
        "multipliers": {"compression": 1.20},
    }
    with tempfile.TemporaryDirectory() as tmp:
        cal_path = Path(tmp) / "expansion_calibration.json"
        cal_path.write_text(json.dumps(payload), encoding="utf-8")
        with patch(
            "hunt_core.analysis.expansion_engine.config.EXPANSION_CALIBRATION_JSON",
            cal_path,
        ):
            invalidate_expansion_config_cache()
            cfg = load_expansion_config()
            expected = round(_DEFAULT_UP_WEIGHTS["compression"] * 1.20, 6)
            if cfg.up_weights.get("compression") != expected:
                return _fail(
                    f"calibration not applied: {cfg.up_weights.get('compression')} != {expected}"
                )
    print("  calibration apply ok")
    return 0


def check_outcome_review() -> int:
    """Pending horizons are graded once; duplicates are skipped."""
    from datetime import UTC, datetime, timedelta

    from hunt_core.analysis.expansion_engine.learning.review import (
        pending_review_horizons,
        review_records_with_prices,
    )

    now = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    signal_ts = (now - timedelta(hours=30)).isoformat()
    rec = {
        "ts": signal_ts,
        "symbol": "TESTUSDT",
        "price": 100.0,
        "dominant": "up",
        "graded": [],
        "execution": {"targets": [110.0], "stop": 95.0},
    }
    pending = pending_review_horizons(rec, now=now)
    if pending != [(24, 30.0)]:
        return _fail(f"expected pending 24h only, got {pending}")

    summary = review_records_with_prices([rec], {"TESTUSDT": 108.0}, now=now)
    if summary.get("graded") != 1:
        return _fail(f"expected 1 grade, got {summary}")
    graded = rec.get("graded") or []
    if len(graded) != 1 or graded[0].get("horizon_h") != 24:
        return _fail(f"unexpected graded payload: {graded}")
    if not graded[0].get("win"):
        return _fail("expected win at +8% favorable move")

    # Re-run — 24h already done, 48h not yet due.
    summary2 = review_records_with_prices([rec], {"TESTUSDT": 108.0}, now=now)
    if summary2.get("graded") != 0:
        return _fail("duplicate grading should be skipped")
    print("  outcome review ok: 24h graded once, no duplicate")
    return 0


def check_import_isolation() -> int:
    root = Path(__file__).resolve().parents[1]
    exp_dir = root / "analysis" / "expansion_engine"
    v2_dir = root / "analysis" / "deep" / "verdict_v2"
    for py in exp_dir.rglob("*.py"):
        if _imports_referencing(py, "verdict_v2"):
            return _fail(f"import isolation: {py} imports verdict_v2")
    for py in v2_dir.rglob("*.py"):
        if _imports_referencing(py, "expansion_engine"):
            return _fail(f"import isolation: {py} imports expansion_engine")
    print("  import isolation ok: zero cross-imports verdict_v2 ↔ expansion_engine")
    return 0


def main() -> int:
    checks = (
        check_trigger_vs_score,
        check_ambiguous_pivot,
        check_delta_momentum,
        check_tier_c_blocks,
        check_strict_mtf,
        check_opportunity_ordering,
        check_rotation_scan,
        check_from_dict_roundtrip,
        check_fsm_persist,
        check_history_persist,
        check_universe_scan,
        check_expansion_alerts,
        check_format_helpers,
        check_config_toml,
        check_calibration_apply,
        check_outcome_review,
        check_import_isolation,
    )
    for fn in checks:
        rc = fn()
        if rc != 0:
            return rc
    print("expansion ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

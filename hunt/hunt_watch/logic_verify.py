"""Synthetic + structural verification for hunt lifecycle / confirm / support."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from hunt_watch.lifecycle import (
    HuntLifecycle,
    HuntPhase,
    apply_short_invalidation,
    assess_hunt_lifecycle,
    blocks_premature_exhaustion_short,
    effective_support_break,
)
from hunt_watch.market_regime import active_params
from hunt_watch.signal_engine import (
    cluster_fuel,
    confirm_dump,
    confirm_long,
    enrich_dump_setup,
    enrich_long_setup,
)


@dataclass(frozen=True, slots=True)
class CaseResult:
    name: str
    ok: bool
    detail: str


def _lc(
    *,
    price: float,
    hunt_high: float,
    hunt_low: float,
    low_24h: float,
    high_24h: float | None = None,
    pos: float | None = None,
    rsi_1h: float = 50.0,
    taker_5m: float | None = None,
    micro: float | None = None,
    c5_bull: bool = False,
    c5_bear: bool = False,
) -> HuntLifecycle:
    hi = high_24h if high_24h is not None else hunt_high
    if pos is None:
        pos = (price - low_24h) / (hi - low_24h) if hi > low_24h else 0.5
    session = {"high_24h": hi, "low_24h": low_24h, "pos_in_range": pos}
    tf: dict[str, Any] = {
        "1h": {"rsi14": rsi_1h, "atr_pct": 8.0},
        "5m_closed": {
            "candle": {
                "bullish": c5_bull,
                "bearish": c5_bear,
                "lower_wick_ratio": 0.3 if c5_bull else 0.0,
                "upper_wick_ratio": 0.3 if c5_bear else 0.0,
            }
        },
        "15m_closed": {"candle": {}, "closed_bar": False},
    }
    market: dict[str, Any] = {}
    if taker_5m is not None:
        market["taker_5m"] = taker_5m
    if micro is not None:
        market["microprice_bias"] = micro
    return assess_hunt_lifecycle(
        price=price,
        hunt_high=hunt_high,
        hunt_low=hunt_low,
        session=session,
        tf=tf,
        market=market,
    )


def run_lifecycle_cases() -> list[CaseResult]:
    results: list[CaseResult] = []

    def check(name: str, lc: HuntLifecycle, *, phase: str, bias: str, entry: bool | None = None) -> None:
        ok = lc.phase.value == phase and lc.recommended_bias == bias
        if entry is not None:
            ok = ok and lc.short_entry_ok == entry
        results.append(
            CaseResult(
                name,
                ok,
                f"got phase={lc.phase.value} bias={lc.recommended_bias} "
                f"entry={lc.short_entry_ok} reasons={lc.reasons}",
            )
        )

    # BEAT +400%: parabolic leg, shallow wick — NOT post_dump_bounce
    lc = _lc(price=5.95, hunt_high=6.36, hunt_low=3.37, low_24h=4.50, taker_5m=1.15)
    check("BEAT_parabolic_distribution", lc, phase="distribution", bias="short", entry=True)

    # BEAT at ATH: exhaustion
    lc = _lc(price=6.30, hunt_high=6.36, hunt_low=3.37, low_24h=4.50, rsi_1h=72, taker_5m=1.1)
    check("BEAT_parabolic_exhaustion", lc, phase="exhaustion_at_high", bias="short", entry=True)

    # BEAT Jun10 continuation 5→8.37 — mega leg, NOT post_dump_bounce
    lc_mega = _lc(
        price=6.0,
        hunt_high=6.5,
        hunt_low=1.5,
        low_24h=4.5,
        high_24h=6.5,
        pos=0.68,
        rsi_1h=58.0,
        taker_5m=1.12,
        c5_bull=True,
    )
    check(
        "BEAT_mega_leg_continuation",
        lc_mega,
        phase="impulse_initiating",
        bias="long",
        entry=False,
    )

    # JCT-style: real dump then bounce
    lc = _lc(
        price=0.42,
        hunt_high=0.50,
        hunt_low=0.30,
        low_24h=0.32,
        taker_5m=1.12,
        c5_bull=True,
    )
    check("JCT_post_dump_bounce", lc, phase="post_dump_bounce", bias="long", entry=False)

    # VELVET-style fade at top
    lc = _lc(price=1.18, hunt_high=1.20, hunt_low=0.90, low_24h=0.92, rsi_1h=74)
    check("VELVET_exhaustion_fade", lc, phase="exhaustion_at_high", bias="short", entry=True)

    # BEAT Jun6-8 initial impulse — mid leg rally, long not short
    lc_imp = _lc(
        price=1.72,
        hunt_high=1.85,
        hunt_low=1.0,
        low_24h=1.0,
        high_24h=1.85,
        pos=0.62,
        rsi_1h=58.0,
        taker_5m=1.12,
        c5_bull=True,
    )
    check(
        "BEAT_initial_impulse_long",
        lc_imp,
        phase="impulse_initiating",
        bias="long",
        entry=False,
    )

    lc_base = assess_hunt_lifecycle(
        price=0.105,
        hunt_high=0.115,
        hunt_low=0.10,
        session={"high_24h": 0.13, "low_24h": 0.09, "pos_in_range": 0.50},
        tf={
            "1h": {
                "rsi14": 48.0,
                "atr_pct": 6.0,
                "bb_width_pctile": 0.15,
                "donchian_width_pct": 7.0,
            },
            "5m_closed": {"candle": {"bullish": False, "bearish": False}, "closed_bar": False},
            "15m_closed": {"candle": {}, "closed_bar": False},
        },
        market={"taker_5m": 1.04},
    )
    results.append(
        CaseResult(
            "VELVET_breakout_arming",
            lc_base.phase.value == "breakout_arming" and lc_base.recommended_bias == "long",
            f"got phase={lc_base.phase.value} bias={lc_base.recommended_bias}",
        )
    )

    # Mid-dump: no new short entry
    lc = _lc(
        price=0.38,
        hunt_high=0.50,
        hunt_low=0.30,
        low_24h=0.35,
        taker_5m=0.92,
        c5_bear=True,
    )
    check("mid_dump_active", lc, phase="dump_active", bias="wait", entry=False)

    # Shallow wick on big leg must NOT be long bounce
    lc = _lc(price=6.0, hunt_high=6.36, hunt_low=2.0, low_24h=2.1, taker_5m=1.2)
    if lc.phase == HuntPhase.POST_DUMP_BOUNCE:
        results.append(CaseResult("no_bounce_on_mega_leg", False, f"wrong {lc.phase}"))
    else:
        results.append(CaseResult("no_bounce_on_mega_leg", True, f"phase={lc.phase.value}"))

    # Support: parabolic distribution fall<8 uses local pivot not ATH
    lc_dist = _lc(price=5.95, hunt_high=6.36, hunt_low=3.37, low_24h=4.50)
    sup = effective_support_break(
        impulse_high=6.36,
        lifecycle=lc_dist,
        pos_in_range=0.75,
    )
    ath_anchor = round(6.36 * 0.998, 6)
    ok = sup < ath_anchor and sup == lc_dist.local_support
    results.append(
        CaseResult(
            "support_parabolic_local_pivot",
            ok,
            f"support={sup} ath_would_be={ath_anchor}",
        )
    )

    # Support: meaningful dump uses ATH anchor when phase is distribution + fall>=8
    lc_dump = _lc(price=0.42, hunt_high=0.50, hunt_low=0.30, low_24h=0.32)
    sup2 = effective_support_break(impulse_high=0.50, lifecycle=lc_dump, pos_in_range=0.6)
    want = round(max(lc_dump.local_support, 0.50 * 0.998), 6)
    results.append(
        CaseResult(
            "support_after_meaningful_dump",
            lc_dump.fall_from_high_pct >= 8.0 and abs(sup2 - want) < 1e-4,
            f"phase={lc_dump.phase.value} fall={lc_dump.fall_from_high_pct} support={sup2} want={want}",
        )
    )

    # JCT premature exhaustion block
    blocked, _ = blocks_premature_exhaustion_short(
        phase="exhaustion_at_high",
        fall_from_high_pct=2.0,
        bounce_from_low_pct=5.0,
        pos_in_range=0.93,
        has_bear_div=False,
    )
    results.append(CaseResult("JCT_premature_exhaustion_block", blocked, "blocked_at_top"))

    blocked2, _ = blocks_premature_exhaustion_short(
        phase="exhaustion_at_high",
        fall_from_high_pct=2.0,
        bounce_from_low_pct=5.0,
        pos_in_range=0.93,
        has_bear_div=False,
    )
    # structural break override tested in watch _should_alert — here just block path
    results.append(CaseResult("JCT_block_without_div", blocked2, "ok"))

    return results


def run_confirm_cases() -> list[CaseResult]:
    cal = active_params()
    results: list[CaseResult] = []
    tf_base: dict[str, Any] = {
        "5m_closed": {"close": 5.8, "candle": {"bearish": True, "upper_wick_ratio": 0.4}},
        "15m_closed": {"close": 5.7, "candle": {"bearish": True, "upper_wick_ratio": 0.4}},
        "1m_closed": {"candle": {"bearish": True, "upper_wick_ratio": 0.4}},
        "1h": {"bearish_rsi_div": False},
        "4h": {"bearish_rsi_div": False},
    }

    def dump_setup(triggers: list[str], support: float, fuel: float) -> dict[str, Any]:
        d = enrich_dump_setup(
            {
                "dump_score": fuel,
                "triggers": triggers,
                "support_break_level": support,
                "levels_viable": True,
                "dump_fuel": fuel,
            }
        )
        return d

    # Two structural closes -> confirm (fuel must survive enrich cluster cap)
    d = dump_setup(["lost_support_6.0", "oi_flush", "crowded_long_funding"], 6.0, 110.0)
    conf, hard = confirm_dump(d, tf_base, symbol="BEATUSDT", price=5.75, cal=cal, lifecycle_bias="short")
    fuel = float(d.get("dump_fuel") or 0)
    results.append(
        CaseResult(
            "confirm_two_structural",
            conf and len(hard) >= 2 and fuel >= cal.confirm_min_score,
            f"hard={hard} fuel={fuel}",
        )
    )

    # One structural + one secondary -> NO confirm
    d2 = dump_setup(["crowded_long_funding"], 6.0, 65.0)
    tf_one = {**tf_base, "5m_closed": {"close": 5.8, "candle": {}}, "15m_closed": {"close": 6.1, "candle": {}}}
    conf2, hard2 = confirm_dump(d2, tf_one, symbol="BEATUSDT", price=5.75, cal=cal, lifecycle_bias="short")
    results.append(CaseResult("confirm_one_structural_insufficient", not conf2, f"hard={hard2}"))

    # Long bias veto
    conf3, hard3 = confirm_dump(d, tf_base, symbol="BEATUSDT", price=5.75, cal=cal, lifecycle_bias="long")
    results.append(CaseResult("confirm_veto_long_bias", not conf3 and "veto" in str(hard3), str(hard3)))

    # Wait bias veto (fade entry) — but dump_active continuation allowed
    d_wait = {**d, "lifecycle_phase": "exhaustion_at_high"}
    conf_wait, hard_wait = confirm_dump(
        d_wait, tf_base, symbol="BEATUSDT", price=5.75, cal=cal, lifecycle_bias="wait"
    )
    results.append(
        CaseResult("confirm_veto_wait_bias", not conf_wait and "wait" in str(hard_wait), str(hard_wait))
    )
    d_dump = {**d, "lifecycle_phase": "dump_active", "fall_from_high_pct": 18.0}
    conf_dump_wait, hard_dump = confirm_dump(
        d_dump, tf_base, symbol="BEATUSDT", price=5.75, cal=cal, lifecycle_bias="wait"
    )
    results.append(
        CaseResult(
            "confirm_dump_active_wait_ok",
            conf_dump_wait and "veto" not in str(hard_dump),
            f"conf={conf_dump_wait} hard={hard_dump}",
        )
    )

    # Fuel floor
    d_low = dump_setup(["lost_support_6.0"], 6.0, 40.0)
    conf4, _ = confirm_dump(d_low, tf_base, symbol="BEATUSDT", price=5.75, cal=cal, lifecycle_bias="short")
    results.append(CaseResult("confirm_fuel_floor", not conf4, f"fuel=40 min={cal.confirm_min_score}"))

    # cluster_fuel dedup
    fuel = cluster_fuel(
        ["rsi15_overbought", "rsi1h_overbought", "bear_div_1h", "crowded_long_funding"],
        raw_score=80.0,
    )
    results.append(CaseResult("fuel_cluster_cap", fuel < 80.0 and fuel <= 100.0, f"fuel={fuel}"))

    # Long confirm veto short bias
    long_d = enrich_long_setup({"long_score": 70, "triggers": ["oi_build"], "levels_viable": True, "long_fuel": 70})
    lconf, lhard = confirm_long(long_d, tf_base, cal=cal, lifecycle_bias="short")
    results.append(CaseResult("long_veto_short_bias", not lconf, str(lhard)))

    # apply_short_invalidation on bounce
    lc_bounce = _lc(price=0.42, hunt_high=0.50, hunt_low=0.30, low_24h=0.32, taker_5m=1.1, c5_bull=True)
    conf5, _, note = apply_short_invalidation(True, ["5m_close_below_support"], lc_bounce, dump={})
    results.append(
        CaseResult(
            "invalidation_on_bounce_phase",
            not conf5 or note is not None,
            f"conf={conf5} note={note}",
        )
    )

    # WLD 2026-06-11 02:00 UTC — 5m close above resistance but tick below break.
    wld_tf = {
        "5m_closed": {"close": 0.46, "candle": {"bullish": True, "lower_wick_ratio": 0.2}},
        "15m_closed": {"close": 0.455, "candle": {}},
        "1m_closed": {"candle": {}},
        "1h": {"plus_di": 12.0, "minus_di": 28.0, "rsi14": 39.3, "adx14": 22.0},
        "1h_closed": {"plus_di": 12.0, "minus_di": 28.0, "rsi14": 39.3, "adx14": 22.0},
    }
    wld_long = enrich_long_setup(
        {
            "long_score": 100,
            "long_fuel": 100,
            "triggers": ["regime_4h_bull", "oi_build"],
            "levels_viable": True,
            "lifecycle_phase": "accumulation",
            "resistance_break_level": 0.458781,
            "fall_from_high_pct": 0.4,
            "context_chg_24h_pct": -10.34,
            "context_pos_in_range": 0.35,
        }
    )
    wld_conf, wld_hard = confirm_long(
        wld_long,
        wld_tf,
        symbol="WLDUSDT",
        price=0.4577,
        cal=cal,
        lifecycle_bias="long",
        lifecycle_phase="accumulation",
    )
    results.append(
        CaseResult(
            "wld_accumulation_long_blocked",
            not wld_conf,
            f"conf={wld_conf} hard={wld_hard}",
        )
    )
    wld_tf_neutral = {
        **wld_tf,
        "1h": {"plus_di": 20.0, "minus_di": 20.0, "rsi14": 50.0, "adx14": 22.0},
        "1h_closed": {"plus_di": 20.0, "minus_di": 20.0, "rsi14": 50.0, "adx14": 22.0},
    }
    wld_retest_conf, wld_retest_hard = confirm_long(
        {**wld_long, "lifecycle_phase": "impulse_initiating"},
        wld_tf_neutral,
        symbol="WLDUSDT",
        price=0.4577,
        cal=cal,
        lifecycle_bias="long",
        lifecycle_phase="impulse_initiating",
    )
    wld_chase_conf, wld_chase_hard = confirm_long(
        {**wld_long, "lifecycle_phase": "impulse_initiating"},
        wld_tf_neutral,
        symbol="WLDUSDT",
        price=0.454,
        cal=cal,
        lifecycle_bias="long",
        lifecycle_phase="impulse_initiating",
    )
    results.append(
        CaseResult(
            "wld_shallow_retest_not_price_veto",
            not any("below_resistance" in str(h) for h in wld_retest_hard),
            f"conf={wld_retest_conf} hard={wld_retest_hard}",
        )
    )
    results.append(
        CaseResult(
            "wld_deep_chase_price_veto",
            not wld_chase_conf and any("below_resistance" in str(h) for h in wld_chase_hard),
            f"conf={wld_chase_conf} hard={wld_chase_hard}",
        )
    )

    from hunt_watch.alert_explain import evaluate_alert_gate

    results.append(
        CaseResult(
            "wld_fuel_capped_weak_accumulation",
            float(wld_long.get("long_fuel") or 0) <= 72.0,
            f"fuel={wld_long.get('long_fuel')}",
        )
    )

    wld_gate = evaluate_alert_gate(
        {**wld_long, "confirmed": True},
        direction="long",
        symbol="WLDUSDT",
        lifecycle={"phase": "accumulation", "recommended_bias": "long"},
        row={
            "price": 0.4577,
            "chg_24h_pct": -10.34,
            "session": {"pos_in_range": 0.35},
        },
    )
    results.append(
        CaseResult(
            "wld_below_resistance_alert_blocked",
            not wld_gate.ok and wld_gate.code == "long_below_resistance",
            f"code={wld_gate.code} msg={wld_gate.message}",
        )
    )

    return results


def run_early_cases() -> list[CaseResult]:
    from hunt_watch.early_alert import evaluate_early_alert

    cal = active_params()
    results: list[CaseResult] = []
    prep = evaluate_early_alert(
        {
            "phase": "dump_setup_forming",
            "dump_fuel": 82,
            "confirmed": False,
            "triggers": ["obv_distribution_at_top"],
        },
        direction="short",
        symbol="BEATUSDT",
        lifecycle={
            "phase": "exhaustion_at_high",
            "short_entry_ok": True,
            "fall_from_high_pct": 1.5,
        },
    )
    results.append(
        CaseResult("early_exhaustion_prep", prep.kind == "prep", prep.message)
    )

    start = evaluate_early_alert(
        {
            "phase": "dump_initiating",
            "dump_fuel": 72,
            "confirmed": False,
            "confirm_hard": ["live_below_support_7.9"],
        },
        direction="short",
        symbol="BEATUSDT",
        lifecycle={
            "phase": "dump_active",
            "short_entry_ok": False,
            "fall_from_high_pct": 5.0,
        },
    )
    results.append(CaseResult("early_dump_start", start.kind == "start", start.message))

    long_prep = evaluate_early_alert(
        {
            "phase": "long_setup_forming",
            "long_fuel": 55,
            "confirmed": False,
        },
        direction="long",
        symbol="BEATUSDT",
        lifecycle={"phase": "post_dump_bounce", "bounce_from_low_pct": 8.0},
    )
    results.append(CaseResult("early_pump_prep", long_prep.kind == "prep", long_prep.message))

    impulse_prep = evaluate_early_alert(
        {
            "phase": "long_setup_forming",
            "long_fuel": 62,
            "confirmed": False,
            "triggers": ["broke_resistance_2.1", "oi_build"],
        },
        direction="long",
        symbol="BEATUSDT",
        lifecycle={
            "phase": "impulse_initiating",
            "bounce_from_low_pct": 35.0,
        },
    )
    results.append(
        CaseResult("early_impulse_pump_start", impulse_prep.kind == "start", impulse_prep.message)
    )

    ign_prep = evaluate_early_alert(
        {"phase": "accumulation_watch", "long_fuel": 48, "confirmed": False},
        direction="long",
        symbol="VELVETUSDT",
        lifecycle={"phase": "impulse_initiating", "bounce_from_low_pct": 22.0},
        row={"ignition": {"active": True, "direction": "pump", "price_delta_pct": 3.2}},
    )
    results.append(CaseResult("early_ignition_pump_prep", ign_prep.kind == "prep", ign_prep.message))

    lconf, lhard = confirm_long(
        enrich_long_setup(
            {
                "long_score": 110,
                "long_fuel": 110,
                "triggers": [
                    "broke_resistance_2.1",
                    "oi_build",
                    "ws_taker_buy_30s",
                    "spot_lead_pump_0.5",
                    "1m_bounce",
                    "5m_bounce",
                    "bull_div_1h",
                ],
                "levels_viable": True,
                "lifecycle_phase": "impulse_initiating",
                "resistance_break_level": 2.05,
            }
        ),
        {
            "5m_closed": {"close": 2.15, "candle": {"bullish": True, "lower_wick_ratio": 0.4}},
            "15m_closed": {"close": 2.10, "candle": {"bullish": True}},
            "1m_closed": {"candle": {"bullish": True, "lower_wick_ratio": 0.4}},
            "1h": {},
        },
        cal=cal,
        lifecycle_bias="long",
        lifecycle_phase="impulse_initiating",
    )
    results.append(
        CaseResult(
            "confirm_long_impulse_initiating",
            lconf and "veto" not in str(lhard),
            f"conf={lconf} hard={lhard}",
        )
    )
    return results


def run_adx_prep_cases() -> list[CaseResult]:
    from hunt_watch.directional_filters import directional_filters

    tf = {
        "1h": {"adx14": 52.0, "plus_di": 35.0, "minus_di": 12.0, "supertrend_dir": 1},
        "15m_closed": {"vwap_dev_atr": 0.5},
    }
    _, short_triggers, short_blocks = directional_filters(
        tf,
        direction="short",
        pos_in_range=0.85,
        symbol="BEATUSDT",
        lifecycle_phase="exhaustion_at_high",
        fall_from_high_pct=2.0,
    )
    tf_long = {
        "1h": {"adx14": 48.0, "plus_di": 10.0, "minus_di": 32.0, "supertrend_dir": -1},
        "15m_closed": {"vwap_dev_atr": 2.0},
    }
    _, long_triggers, long_blocks = directional_filters(
        tf_long,
        direction="long",
        pos_in_range=0.35,
        symbol="BEATUSDT",
        lifecycle_phase="post_dump_bounce",
        fall_from_high_pct=18.0,
    )
    _, weak_triggers, _ = directional_filters(
        tf_long,
        direction="long",
        pos_in_range=0.35,
        symbol="BEATUSDT",
        lifecycle_phase="accumulation",
        fall_from_high_pct=0.4,
        chg_24h_pct=-10.0,
    )
    tf_vwap_block = {
        "1h": {"adx14": 20.0, "plus_di": 15.0, "minus_di": 15.0},
        "15m_closed": {"vwap_dev_atr": 2.6},
    }
    _, _, vwap_blocks = directional_filters(
        tf_vwap_block,
        direction="long",
        pos_in_range=0.6,
        lifecycle_phase="distribution",
    )
    tf_vwap_dump = {
        "1h": {"adx14": 22.0, "plus_di": 18.0, "minus_di": 20.0},
        "15m_closed": {"vwap_dev_atr": -4.2},
    }
    _, dump_triggers, dump_blocks = directional_filters(
        tf_vwap_dump,
        direction="short",
        pos_in_range=0.35,
        lifecycle_phase="dump_active",
        fall_from_high_pct=18.0,
    )
    return [
        CaseResult(
            "adx_soft_exhaustion_fade",
            not short_blocks and any("fade_prep_soft" in t for t in short_triggers),
            f"blocks={short_blocks} triggers={short_triggers}",
        ),
        CaseResult(
            "adx_soft_pump_bounce",
            not long_blocks and any("pump_prep_soft" in t for t in long_triggers),
            f"blocks={long_blocks} triggers={long_triggers}",
        ),
        CaseResult(
            "weak_accumulation_soft_penalty",
            any("weak_accumulation_soft" in t for t in weak_triggers),
            str(weak_triggers),
        ),
        CaseResult(
            "vwap_extreme_225_blocks",
            any("vwap_overbought" in b for b in vwap_blocks),
            str(vwap_blocks),
        ),
        CaseResult(
            "vwap_oversold_soft_dump_leg",
            not dump_blocks and any("dump_leg_soft" in t for t in dump_triggers),
            f"blocks={dump_blocks} triggers={dump_triggers}",
        ),
    ]


def run_mtf_cases() -> list[CaseResult]:
    from hunt_watch.mtf_policy import mtf_confirm_veto

    tf_closed = {
        "5m_closed": {"close": 5.8, "candle": {}},
        "15m_closed": {"close": 5.7, "candle": {}},
    }
    blocked_bounce, reason_bounce = mtf_confirm_veto(
        "short", tf_closed, "post_dump_bounce"
    )
    tf_bull = {
        **tf_closed,
        "1h": {"plus_di": 40.0, "minus_di": 10.0, "adx14": 35.0},
    }
    blocked_bull, _ = mtf_confirm_veto(
        "short",
        tf_bull,
        "exhaustion_at_high",
        fall_from_high_pct=5.0,
    )
    blocked_dump, _ = mtf_confirm_veto(
        "short",
        tf_bull,
        "dump_active",
        fall_from_high_pct=18.0,
    )
    tf_wld_bear = {
        "5m_closed": {"close": 0.46, "candle": {}},
        "15m_closed": {"close": 0.455, "candle": {}},
        "1h_closed": {
            "plus_di": 12.0,
            "minus_di": 28.0,
            "rsi14": 39.3,
            "adx14": 22.0,
        },
    }
    blocked_wld_acc, reason_wld = mtf_confirm_veto(
        "long",
        tf_wld_bear,
        "accumulation",
        fall_from_high_pct=0.4,
    )
    blocked_bounce_rec, reason_rec = mtf_confirm_veto(
        "short",
        tf_closed,
        "recovery",
        fall_from_high_pct=6.0,
        bounce_from_low_pct=12.0,
    )
    cal = active_params()
    d_bounce = enrich_dump_setup(
        {
            "dump_score": 110,
            "dump_fuel": 110,
            "triggers": ["lost_support_6.0", "oi_flush"],
            "support_break_level": 6.0,
            "levels_viable": True,
            "lifecycle_phase": "post_dump_bounce",
            "fall_from_high_pct": 20.0,
        }
    )
    conf_bounce, hard_bounce = confirm_dump(
        d_bounce,
        tf_bull,
        symbol="BEATUSDT",
        price=5.75,
        cal=cal,
        lifecycle_bias="short",
    )
    return [
        CaseResult(
            "mtf_bounce_short_blocked",
            blocked_bounce and "bounce" in reason_bounce,
            reason_bounce,
        ),
        CaseResult("mtf_1h_bull_blocks_fade", blocked_bull, "bull vs short fade"),
        CaseResult("mtf_dump_active_allows_bull", not blocked_dump, "dump continuation"),
        CaseResult(
            "confirm_veto_post_dump_bounce",
            not conf_bounce and any("mtf" in str(h) for h in hard_bounce),
            str(hard_bounce),
        ),
        CaseResult(
            "mtf_bear_1h_blocks_wld_accumulation",
            blocked_wld_acc and "accumulation" in reason_wld,
            reason_wld,
        ),
        CaseResult(
            "mtf_bounce_recovery_blocks_short",
            blocked_bounce_rec and "bounce_recovery" in reason_rec,
            reason_rec,
        ),
    ]


def run_btc_corr_cases() -> list[CaseResult]:
    from hunt_watch.btc_alignment import correlated_direction

    # Live SOXL probe: corr≈0.67 — soft tier, wide fuel gap keeps raw short.
    soft_dir, soft_notes = correlated_direction(
        short_fuel=70.0,
        long_fuel=55.0,
        btc_corr_1h=0.6678,
        btc_trend="up",
        symbol="SOXLUSDT",
    )
    # Hard tier: corr≥0.70, moderate gap flips to BTC-aligned long.
    hard_dir, hard_notes = correlated_direction(
        short_fuel=70.0,
        long_fuel=55.0,
        btc_corr_1h=0.75,
        btc_trend="up",
        symbol="TESTUSDT",
    )
    low_dir, _ = correlated_direction(
        short_fuel=60.0,
        long_fuel=40.0,
        btc_corr_1h=0.21,
        btc_trend="up",
        symbol="BTWUSDT",
    )
    return [
        CaseResult(
            "btc_soft_keeps_strong_fuel",
            soft_dir == "short" and any("soft" in n for n in soft_notes),
            f"dir={soft_dir} notes={soft_notes[:2]}",
        ),
        CaseResult(
            "btc_hard_aligns_long",
            hard_dir == "long" and any("hard" in n for n in hard_notes),
            f"dir={hard_dir}",
        ),
        CaseResult(
            "btc_low_corr_no_filter",
            low_dir == "short",
            f"dir={low_dir}",
        ),
    ]


def run_frame_fallback_cases() -> list[CaseResult]:
    import polars as pl

    from hunt_watch.frame_fallback import patch_work_4h, synth_4h_from_1h

    rows = []
    for i in range(60):
        rows.append(
            {
                "open_time": i * 3_600_000,
                "open": 100.0 + i * 0.1,
                "high": 101.0 + i * 0.1,
                "low": 99.0 + i * 0.1,
                "close": 100.5 + i * 0.1,
                "volume": 1000.0,
            }
        )
    df_1h = pl.DataFrame(rows)
    synth = synth_4h_from_1h(df_1h)
    class _P:
        work_4h = None
        work_1h = df_1h

    stub = _P()
    patched = patch_work_4h(stub, {"1h": df_1h})
    w4 = getattr(stub, "work_4h", None)
    h = w4.height if w4 is not None and hasattr(w4, "height") else 0
    return [
        CaseResult(
            "synth_4h_from_1h",
            synth is not None and synth.height >= 12,
            f"bars={synth.height if synth is not None else 0}",
        ),
        CaseResult(
            "patch_work_4h",
            patched and h >= 12,
            f"patched={patched} h={h}",
        ),
    ]


def run_phase_matrix_cases() -> list[CaseResult]:
    from unittest.mock import patch

    from hunt_watch.alert_explain import evaluate_alert_gate
    from hunt_watch.param_store import phase_matrix_thresholds
    from hunt_watch.phase_matrix_gate import PhaseStats

    pm = phase_matrix_thresholds()
    min_n = int(pm.get("min_samples", 12))
    max_wr = float(pm.get("max_wr", 0.28))
    prior_wr = float(pm.get("prior_wr", 0.35))
    st = PhaseStats("exhaustion_at_high", "short", wins=2, losses=10)
    n0 = 4.0
    adj_wr = (st.wins + prior_wr * n0) / (st.n + n0)
    disable_ok = st.n >= min_n and adj_wr < max_wr

    setup = {
        "confirmed": True,
        "dump_fuel": 90.0,
        "dump_score": 90.0,
        "filter_blocks": [],
        "levels_viable": True,
        "risk_reward": 2.0,
        "tp2": 1.0,
    }
    lifecycle = {
        "phase": "exhaustion_at_high",
        "recommended_bias": "short",
        "short_entry_ok": True,
        "invalidate_short": False,
        "fall_from_high_pct": 12.0,
        "bounce_from_low_pct": 3.0,
    }
    row = {
        "price": 10.0,
        "chg_24h_pct": 20.0,
        "session": {"range_pct_24h": 25.0},
        "young_listing": False,
    }

    with patch(
        "hunt_watch.alert_explain.phase_matrix_gate",
        return_value=(True, "phase auto-off test"),
    ):
        gate = evaluate_alert_gate(
            setup,
            direction="short",
            symbol="BEATUSDT",
            lifecycle=lifecycle,
            row=row,
        )

    return [
        CaseResult(
            "phase_matrix_stats_threshold",
            disable_ok,
            f"n={st.n} wr={st.wr:.0%} adj_wr={adj_wr:.0%}",
        ),
        CaseResult(
            "phase_matrix_blocks_confirmed",
            not gate.ok and gate.code == "phase_matrix_disable",
            gate.message,
        ),
    ]


def run_lifecycle_sticky_cases() -> list[CaseResult]:
    from hunt_watch.lifecycle import HuntLifecycle, HuntPhase
    from hunt_watch.lifecycle_sticky import reset_symbol, stabilize

    reset_symbol("WLDUSDT")
    base = HuntLifecycle(
        phase=HuntPhase.ACCUMULATION,
        recommended_bias="long",
        short_entry_ok=False,
        short_confirm_ok=False,
        invalidate_short=False,
        fall_from_high_pct=5.0,
        bounce_from_low_pct=12.0,
        local_support=0.4,
        local_resistance=0.5,
        reasons=("test",),
    )
    dump_raw = HuntLifecycle(
        phase=HuntPhase.DUMP_ACTIVE,
        recommended_bias="wait",
        short_entry_ok=False,
        short_confirm_ok=True,
        invalidate_short=False,
        fall_from_high_pct=9.0,
        bounce_from_low_pct=8.0,
        local_support=0.4,
        local_resistance=0.5,
        reasons=("dump_tick",),
    )
    stabilize("WLDUSDT", base)
    s1 = stabilize("WLDUSDT", dump_raw)
    s2 = stabilize("WLDUSDT", dump_raw)
    s3 = stabilize("WLDUSDT", dump_raw)
    s4 = stabilize("WLDUSDT", dump_raw)
    reset_symbol("WLDUSDT")

    return [
        CaseResult(
            "sticky_holds_first_dump_tick",
            s1.phase == HuntPhase.ACCUMULATION,
            f"phase={s1.phase.value}",
        ),
        CaseResult(
            "sticky_flips_after_n_dump_ticks",
            s4.phase == HuntPhase.DUMP_ACTIVE,
            f"phase={s4.phase.value}",
        ),
    ]


def run_phase_change_policy_cases() -> list[CaseResult]:
    from datetime import UTC, datetime, timedelta

    from hunt_watch.signal_tracker import evaluate_followups

    state = {"signals": {}, "followup_sent": {}}
    opened = datetime.now(UTC) - timedelta(minutes=5)
    state["signals"]["WLDUSDT:long"] = {
        "status": "active",
        "opened_at": opened.isoformat(),
        "direction": "long",
        "entry_lifecycle_phase": "accumulation",
        "entry_lifecycle_bias": "long",
        "lifecycle_phase": "accumulation",
        "lifecycle_bias": "long",
        "telegram_sent": True,
        "entry_lo": 0.44,
        "entry_hi": 0.46,
        "stop_loss": 0.42,
        "tp1": 0.48,
        "tp2": 0.50,
        "extreme_hi": 0.46,
        "extreme_lo": 0.45,
    }
    row_wait = {
        "symbol": "WLDUSDT",
        "price": 0.455,
        "lifecycle": {"phase": "dump_active", "recommended_bias": "wait"},
        "long": {"phase": "long_confirmed", "confirmed": True},
        "timeframes": {"5m_closed": {"candle": {"high": 0.456, "low": 0.454}}},
    }
    ev_wait = evaluate_followups(state, row_wait, now=datetime.now(UTC))
    row_back = {
        **row_wait,
        "lifecycle": {"phase": "accumulation", "recommended_bias": "long"},
    }
    ev_back = evaluate_followups(state, row_back, now=datetime.now(UTC))
    phase_msgs = [e for e in ev_wait + ev_back if e.event == "phase_change"]
    return [
        CaseResult(
            "no_phase_change_on_long_to_wait",
            len(phase_msgs) == 0,
            f"events={[e.detail for e in phase_msgs]}",
        ),
        CaseResult(
            "signal_stays_active_through_wait_flicker",
            state["signals"]["WLDUSDT:long"]["status"] == "active",
            state["signals"]["WLDUSDT:long"]["status"],
        ),
    ]


def run_ws_research_cases() -> list[CaseResult]:
    """Reports 24/25: nq orderflow + ap mark spread."""
    import collections
    from unittest.mock import patch

    from hunt_core.market.streams import HuntCcxtStreams, _AggPoint
    from hunt_core.market import HuntCcxtClient

    feed = HuntCcxtStreams(client=HuntCcxtClient())
    sym = "BTCUSDT"
    now = 1_700_000_000_000
    feed._agg_points[sym] = collections.deque(
        [
            _AggPoint(ts_ms=now, qty=10.0, qty_full=20.0, is_buy=True),
            _AggPoint(ts_ms=now, qty=30.0, qty_full=30.0, is_buy=False),
        ],
        maxlen=100,
    )
    with patch("hunt_core.market.streams.time") as tm:
        tm.time.return_value = now / 1000.0
        nq_delta = feed.agg_trade_delta(sym, window_seconds=60, use_nq=True)
        q_delta = feed.agg_trade_delta(sym, window_seconds=60, use_nq=False)
        rpi = feed.agg_rpi_skew(sym, window_seconds=60)

    feed._mark_state[sym] = (now, 100.0, 99.0, 0.0001, 99.5)
    with patch("hunt_core.market.streams.time") as tm:
        tm.time.return_value = now / 1000.0
        snap = feed.mark_snapshot(sym) or {}

    return [
        CaseResult(
            "ws_nq_prefers_normal_qty",
            nq_delta is not None and nq_delta < (q_delta or 1.0),
            f"nq={nq_delta} q={q_delta}",
        ),
        CaseResult(
            "ws_rpi_skew_detected",
            rpi is not None and rpi > 0.0,
            f"rpi_skew={rpi}",
        ),
        CaseResult(
            "ws_mark_ap_spread",
            snap.get("mark_ap_spread_bps") is not None
            and float(snap["mark_ap_spread_bps"]) > 0,
            f"spread={snap.get('mark_ap_spread_bps')}",
        ),
    ]


def run_ensemble_cases() -> list[CaseResult]:
    from hunt_watch.mtf_policy import mtf_confirm_veto
    from hunt_watch.regime_ensemble import classify

    tf_squeeze = {
        "1h_closed": {
            "adx14": 12.0,
            "atr_pct": 2.5,
            "squeeze_on": True,
            "bb_width_pctile": 0.15,
        },
        "5m_closed": {"close": 1.0, "candle": {}},
        "15m_closed": {"close": 1.0, "candle": {}},
    }
    ens = classify(tf_squeeze, trend_1h="neutral")

    tf_chop = {
        "1h_closed": {
            "adx14": 12.0,
            "atr_pct": 6.0,
            "squeeze_on": False,
            "plus_di": 10.0,
            "minus_di": 28.0,
        },
        "5m_closed": {"close": 1.0, "candle": {}},
        "15m_closed": {"close": 1.0, "candle": {}},
    }
    blocked_long, reason_long = mtf_confirm_veto(
        "long",
        tf_chop,
        "breakout_arming",
        market={},
    )
    tf_basis = {
        "1h_closed": {
            "adx14": 22.0,
            "atr_pct": 3.0,
            "squeeze_on": False,
            "plus_di": 32.0,
            "minus_di": 12.0,
        },
        "5m_closed": {"close": 1.0, "candle": {}},
        "15m_closed": {"close": 1.0, "candle": {}},
    }
    blocked_basis, reason_basis = mtf_confirm_veto(
        "long",
        tf_basis,
        "distribution",
        market={"basis_ap_bps": 130.0},
    )

    return [
        CaseResult(
            "ensemble_squeeze_label",
            ens.label == "squeeze",
            f"label={ens.label}",
        ),
        CaseResult(
            "mtf_basis_ap_blocks_long",
            blocked_basis and "basis_ap" in reason_basis,
            reason_basis,
        ),
        CaseResult(
            "mtf_volatile_chop_blocks_bear_long",
            blocked_long and "volatile_chop" in reason_long,
            reason_long,
        ),
    ]


def run_orderflow_cases() -> list[CaseResult]:
    from hunt_watch.level_calibration import adaptive_level_params
    from hunt_watch.levels import _phase_min_rr_long
    from hunt_watch.signal_engine import (
        confirm_dump,
        confirm_long,
        enrich_dump_setup,
        enrich_long_setup,
        long_resistance_chase_veto,
    )

    cal = active_params()
    tf_short: dict[str, Any] = {
        "5m_closed": {"close": 5.8, "candle": {"bearish": True, "upper_wick_ratio": 0.4}},
        "15m_closed": {"close": 5.7, "candle": {"bearish": True, "upper_wick_ratio": 0.4}},
        "1m_closed": {"candle": {"bearish": True, "upper_wick_ratio": 0.4}},
        "1h": {"bearish_rsi_div": False},
        "4h": {"bearish_rsi_div": False},
    }
    dump = enrich_dump_setup(
        {
            "dump_score": 110,
            "dump_fuel": 110,
            "triggers": ["lost_support_6.0", "oi_flush", "crowded_long_funding"],
            "support_break_level": 6.0,
            "levels_viable": True,
        }
    )
    conf_aligned, _ = confirm_dump(
        dump,
        tf_short,
        symbol="BEATUSDT",
        price=5.75,
        market={"agg_trade_delta_60s": 0.35},
        cal=cal,
        lifecycle_bias="short",
    )
    conf_mis, hard_mis = confirm_dump(
        dump,
        tf_short,
        symbol="BEATUSDT",
        price=5.75,
        market={"agg_trade_delta_60s": 0.65},
        cal=cal,
        lifecycle_bias="short",
    )
    tf_long: dict[str, Any] = {
        "5m_closed": {"close": 0.47, "candle": {"bullish": True, "lower_wick_ratio": 0.4}},
        "15m_closed": {"close": 0.465, "candle": {"bullish": True, "lower_wick_ratio": 0.35}},
        "1m_closed": {"candle": {"bullish": True, "lower_wick_ratio": 0.4}},
        "1h": {"plus_di": 28.0, "minus_di": 12.0, "rsi14": 55.0, "adx14": 22.0},
        "1h_closed": {"plus_di": 28.0, "minus_di": 12.0, "rsi14": 55.0, "adx14": 22.0},
    }
    long_d = enrich_long_setup(
        {
            "long_score": 120,
            "long_fuel": 120,
            "triggers": [
                "oi_build",
                "broke_resistance_0.46",
                "ws_taker_buy_60s",
                "spot_lead_pump_0.50",
            ],
            "levels_viable": True,
            "lifecycle_phase": "impulse_initiating",
            "resistance_break_level": 0.46,
        }
    )
    lconf_ok, _ = confirm_long(
        long_d,
        tf_long,
        symbol="VELVETUSDT",
        price=0.471,
        market={"agg_trade_delta_60s": 0.62},
        cal=cal,
        lifecycle_bias="long",
        lifecycle_phase="impulse_initiating",
    )
    lconf_bad, lhard_bad = confirm_long(
        long_d,
        tf_long,
        symbol="VELVETUSDT",
        price=0.471,
        market={"agg_trade_delta_60s": 0.48},
        cal=cal,
        lifecycle_bias="long",
        lifecycle_phase="impulse_initiating",
    )
    sl_dist = adaptive_level_params(lifecycle_phase="distribution").sl_max_atr
    sl_bounce = adaptive_level_params(lifecycle_phase="accumulation").sl_max_atr
    hot_impulse = adaptive_level_params(
        range_pct_24h=65.0, lifecycle_phase="impulse_initiating"
    )
    return [
        CaseResult("orderflow_short_aligned", conf_aligned, "60s sell ok"),
        CaseResult(
            "orderflow_short_buy_veto",
            not conf_mis and "orderflow" in str(hard_mis),
            str(hard_mis),
        ),
        CaseResult("orderflow_long_aligned", lconf_ok, "60s buy ok"),
        CaseResult(
            "orderflow_long_sell_veto",
            not lconf_bad and "orderflow" in str(lhard_bad),
            str(lhard_bad),
        ),
        CaseResult("sl_atr_distribution_cap", sl_dist <= 2.25, f"sl_max_atr={sl_dist}"),
        CaseResult(
            "sl_atr_accumulation_floor",
            2.0 <= sl_bounce <= 2.25,
            f"sl_max_atr={sl_bounce}",
        ),
        CaseResult(
            "impulse_hot_sl_pct_bump",
            hot_impulse.sl_max_pct >= 12.0,
            f"sl_max_pct={hot_impulse.sl_max_pct}",
        ),
        CaseResult(
            "long_retest_shallow_ok",
            not long_resistance_chase_veto(1.0, 0.996, 1.001),
            "0.4% retest after 5m break",
        ),
        CaseResult(
            "long_retest_deep_blocked",
            long_resistance_chase_veto(1.0, 0.99, 1.001),
            "1% below break",
        ),
        CaseResult(
            "pump_start_min_rr_long",
            _phase_min_rr_long("impulse_initiating") == 0.85,
            "impulse 0.85",
        ),
    ]


def run_replay_cases() -> list[CaseResult]:
    from hunt_watch.jsonl_replay import (
        gate_lifecycle_phase,
        load_tick_rows,
        pick_recommended_floor,
        resolve_tick_paths,
        row_has_closed_bars,
        sweep_confirm_min,
        walk_forward_sweep,
    )
    from hunt_watch.paths import DATA, TICK_JSONL

    good = {
        "symbol": "BEATUSDT",
        "price": 5.8,
        "dump": {"dump_score": 80, "dump_fuel": 80},
        "timeframes": {
            "5m_closed": {"close": 5.8},
            "15m_closed": {"close": 5.7},
        },
    }
    bad = {**good, "timeframes": {"5m": {"close": 5.8}}}
    rows = [good, bad, good]
    closed_ok = row_has_closed_bars(good) and not row_has_closed_bars(bad)
    buckets = sweep_confirm_min([good], floors=(60, 70))
    picked = pick_recommended_floor(buckets)
    wf = walk_forward_sweep([good] * 50, min_oos_ticks=10)
    long_gate_phase = gate_lifecycle_phase(
        direction="long",
        stored_phase="distribution",
        recomp_phase="impulse_initiating",
    )
    short_gate_phase = gate_lifecycle_phase(
        direction="short",
        stored_phase="dump_active",
        recomp_phase="impulse_initiating",
    )
    explicit_paths = resolve_tick_paths([DATA / "dump_minute_watch-2026-06-11.jsonl"])
    return [
        CaseResult("replay_closed_bar_gate", closed_ok, "5m/15m closed required"),
        CaseResult(
            "replay_paths_include_staging_buffer",
            TICK_JSONL in explicit_paths,
            str(len(explicit_paths)),
        ),
        CaseResult(
            "replay_long_pump_phase_over_stale_distribution",
            long_gate_phase == "impulse_initiating",
            long_gate_phase,
        ),
        CaseResult(
            "replay_short_keeps_stored_dump_active",
            short_gate_phase == "dump_active",
            short_gate_phase,
        ),
        CaseResult("replay_sweep_picks_floor", picked is not None, f"floor={getattr(picked, 'confirm_min', None)}"),
        CaseResult(
            "replay_walk_forward_insufficient",
            wf.get("error") == "insufficient_rows",
            str(wf.get("n_rows")),
        ),
    ]


def run_sniper_cases() -> list[CaseResult]:
    from hunt_watch.deliver.sniper import SniperConfig, sniper_block_reason

    cfg = SniperConfig(
        enabled=True,
        live_phases=frozenset({"dump_active"}),
        top_ls_max=2.0,
        require_top_ls=True,
        chase_tol=0.002,
    )
    setup = {"entry_zone": [0.99, 1.01]}
    lc_ok = {"phase": "dump_active", "short_entry_ok": True}
    row_ok = {"price": 1.0, "market": {"top_ls_1h": 1.5}}
    allow = sniper_block_reason(
        direction="short", setup=setup, row=row_ok, lifecycle=lc_ok, config=cfg
    )
    block_long = sniper_block_reason(
        direction="long", setup=setup, row=row_ok, lifecycle=lc_ok, config=cfg
    )
    block_phase = sniper_block_reason(
        direction="short",
        setup=setup,
        row=row_ok,
        lifecycle={"phase": "impulse_initiating", "short_entry_ok": True},
        config=cfg,
    )
    block_ls = sniper_block_reason(
        direction="short",
        setup=setup,
        row={"price": 1.0, "market": {"top_ls_1h": 2.5}},
        lifecycle=lc_ok,
        config=cfg,
    )
    return [
        CaseResult("sniper_allows_dump_active_short", allow is None, allow or "ok"),
        CaseResult("sniper_blocks_long_shadow", block_long == "sniper_long_shadow", block_long or ""),
        CaseResult("sniper_blocks_wrong_phase", block_phase is not None, block_phase or ""),
        CaseResult("sniper_blocks_top_ls_high", block_ls == "sniper_top_ls_high", block_ls or ""),
    ]


def run_delivery_cases() -> list[CaseResult]:
    from hunt_watch.alert_explain import evaluate_alert_gate

    base_row = {
        "price": 5.8,
        "chg_24h_pct": 25.0,
        "session": {"range_pct_24h": 30.0, "pos_in_range": 0.92},
        "timeframes": {"1h": {}, "4h": {}},
        "lifecycle": {"phase": "exhaustion_at_high", "recommended_bias": "short", "short_entry_ok": True},
    }
    weak_short = {
        "confirmed": True,
        "dump_fuel": 71.0,
        "dump_score": 71.0,
        "confirm_hard": ["5m_rejection_exhaustion"],
        "filter_blocks": [],
        "levels_viable": True,
        "risk_reward": 1.3,
        "tp2": 5.0,
        "stop_loss": 6.2,
    }
    gate_weak = evaluate_alert_gate(
        weak_short, direction="short", symbol="BEATUSDT", lifecycle=base_row["lifecycle"], row=base_row
    )
    strong_short = {
        **weak_short,
        "dump_fuel": 82.0,
        "dump_score": 82.0,
        "confirm_hard": ["5m_close_below_support", "5m_rejection_exhaustion"],
    }
    gate_strong = evaluate_alert_gate(
        strong_short, direction="short", symbol="BEATUSDT", lifecycle=base_row["lifecycle"], row=base_row
    )
    return [
        CaseResult("delivery_blocks_low_fuel", not gate_weak.ok, gate_weak.code),
        CaseResult("delivery_passes_high_confluence", gate_strong.ok and gate_strong.code == "ok", gate_strong.code),
    ]


def run_prep_shadow_cases() -> list[CaseResult]:
    from hunt_watch.prep_shadow_tracker import (
        process_prep_shadow,
        summarize_prep_shadows,
    )

    state: dict = {"active": {}, "closed": [], "cooldowns": {}}
    now = datetime.now(UTC)
    lc = {
        "phase": "exhaustion_at_high",
        "recommended_bias": "short",
        "short_entry_ok": True,
        "fall_from_high_pct": 8.0,
    }
    row = {
        "symbol": "BEATUSDT",
        "price": 10.0,
        "chg_24h_pct": 40.0,
        "session": {"range_pct_24h": 35.0, "pos_in_range": 0.9},
        "timeframes": {"5m_closed": {"close": 10.0, "candle": {"high": 10.1, "low": 9.9}}},
    }
    setup = {
        "dump_fuel": 55.0,
        "dump_score": 55.0,
        "phase": "exhaustion_watch",
        "confirmed": False,
        "confirm_hard": [],
        "triggers": ["rsi15_overbought"],
        "stop_loss": 10.5,
        "tp1": 9.5,
        "levels_viable": True,
    }
    process_prep_shadow(
        state,
        symbol="BEATUSDT",
        direction="short",
        setup=setup,
        row=row,
        lifecycle=lc,
        now=now,
    )
    opened = bool(state.get("active"))
    row2 = {
        **row,
        "price": 9.6,
        "timeframes": {"5m_closed": {"close": 9.6, "candle": {"high": 10.0, "low": 9.5}}},
    }
    process_prep_shadow(
        state,
        symbol="BEATUSDT",
        direction="short",
        setup=setup,
        row=row2,
        lifecycle=lc,
        now=now + timedelta(hours=9),
    )
    summary = summarize_prep_shadows(state)
    active = state.get("active") or {}
    mfe = float((list(active.values())[0] if active else state["closed"][-1]).get("mfe_pct") or 0)
    return [
        CaseResult("prep_shadow_opens", opened, f"active={len(state.get('active') or {})}"),
        CaseResult(
            "prep_shadow_tracks_mfe",
            mfe >= 3.0 or summary.n_closed >= 1,
            f"mfe={mfe} closed={summary.n_closed}",
        ),
    ]


def run_delivery_regime_cases() -> list[CaseResult]:
    from hunt_watch.alert_explain import _delivery_quality_gate
    from hunt_watch.prep_shadow_tracker import prep_shadow_delivery_fuel_adjustment

    lc_exh = {
        "phase": "exhaustion_at_high",
        "recommended_bias": "short",
        "short_entry_ok": True,
        "fall_from_high_pct": 6.0,
    }
    setup_short = {
        "confirm_hard": ["rejection_wick_15m", "ws_liq_cascade_short"],
        "levels_viable": True,
        "risk_reward": 1.4,
    }
    row_adx = {
        "symbol": "BEATUSDT",
        "price": 10.0,
        "chg_24h_pct": 45.0,
        "session": {"range_pct_24h": 40.0, "pos_in_range": 0.92},
        "timeframes": {"1h": {"adx14": 36.0}},
    }
    gate_adx = _delivery_quality_gate(
        setup_short,
        direction="short",
        symbol="BEATUSDT",
        lifecycle=lc_exh,
        fuel=82.0,
        row=row_adx,
    )

    lc_pump = {"phase": "impulse_initiating", "recommended_bias": "long"}
    setup_long = {
        "confirm_hard": ["5m_close_above", "ws_taker_buy"],
        "levels_viable": True,
        "risk_reward": 1.3,
    }
    row_weak = {
        "symbol": "VELVETUSDT",
        "price": 0.80,
        "chg_24h_pct": 50.0,
        "session": {"high_24h": 1.0, "low_24h": 0.70, "pos_in_range": 0.33, "range_pct_24h": 42.0},
        "timeframes": {"1h": {"adx14": 24.0}},
    }
    gate_long = _delivery_quality_gate(
        setup_long,
        direction="long",
        symbol="VELVETUSDT",
        lifecycle=lc_pump,
        fuel=76.0,
        row=row_weak,
    )

    shadow_state: dict = {
        "active": {},
        "closed": [
            {"direction_correct": False, "tier": "prep", "mfe_pct": 0.5}
            for _ in range(10)
        ],
        "cooldowns": {},
    }
    bump, reason = prep_shadow_delivery_fuel_adjustment(shadow_state)
    gate_oi = _delivery_quality_gate(
        {
            "confirm_hard": ["5m_close_above", "ws_taker_buy"],
            "levels_viable": True,
            "risk_reward": 1.3,
        },
        direction="long",
        symbol="VELVETUSDT",
        lifecycle={"phase": "impulse_initiating", "recommended_bias": "long"},
        fuel=76.0,
        row={
            "price": 0.95,
            "chg_24h_pct": 80.0,
            "session": {
                "high_24h": 1.0,
                "low_24h": 0.7,
                "pos_in_range": 0.83,
                "range_pct_24h": 50.0,
            },
            "market": {"oi_chg_1h": 0.001},
            "timeframes": {"1h": {"adx14": 22.0}},
        },
    )

    return [
        CaseResult(
            "delivery_blocks_strong_adx_fade",
            gate_adx is not None and gate_adx.code == "exhaustion_strong_trend",
            gate_adx.code if gate_adx else "ok",
        ),
        CaseResult(
            "delivery_blocks_weak_impulse_long",
            gate_long is not None and gate_long.code == "impulse_session_weak",
            gate_long.code if gate_long else "ok",
        ),
        CaseResult(
            "prep_shadow_tightens_delivery",
            bump >= 3.0 and reason is not None,
            f"bump={bump} {reason}",
        ),
        CaseResult(
            "prep_bump_waived_confirmed_structural_dump",
            _delivery_quality_gate(
                {
                    "confirmed": True,
                    "confirm_hard": ["5m_close_below_support", "1m_5m_bear_cascade"],
                    "levels_viable": True,
                    "risk_reward": 1.3,
                },
                direction="short",
                symbol="BEATUSDT",
                lifecycle={
                    "phase": "dump_active",
                    "fall_from_high_pct": 18.0,
                    "short_entry_ok": True,
                },
                fuel=73.0,
                row={
                    "price": 9.0,
                    "chg_24h_pct": 80.0,
                    "session": {"range_pct_24h": 50.0},
                },
            )
            is None,
            "fuel73 structural dump passes delivery",
        ),
        CaseResult(
            "delivery_blocks_weak_oi_long",
            gate_oi is not None and gate_oi.code == "impulse_oi_weak",
            gate_oi.code if gate_oi else "ok",
        ),
    ]


def run_dump_continuation_cases() -> list[CaseResult]:
    from hunt_watch.alert_explain import (
        _dump_continuation_short_ok,
        _tp2_room_blocks,
        evaluate_alert_gate,
    )

    setup = {
        "confirmed": True,
        "confirm_hard": ["5m_close_below_support", "ws_liq_cascade_long_flush"],
        "dump_fuel": 84.0,
        "dump_score": 84.0,
        "levels_viable": True,
        "risk_reward": 1.3,
        "tp1": 8.5,
        "tp2": 8.45,
        "stop_loss": 9.2,
    }
    lc = {
        "phase": "impulse_initiating",
        "fall_from_high_pct": 17.5,
        "short_entry_ok": False,
        "recommended_bias": "long",
    }
    row = {
        "price": 9.0,
        "chg_24h_pct": 80.0,
        "session": {"range_pct_24h": 50.0, "pos_in_range": 0.4},
    }
    lc_dump = {
        **lc,
        "phase": "dump_active",
        "fall_from_high_pct": 18.0,
        "short_entry_ok": True,
    }
    cont = _dump_continuation_short_ok(
        setup, phase="impulse_initiating", lc=lc, fuel=84.0, cal_min_fuel=72.0
    )
    low_fuel = {**setup, "dump_fuel": 64.0, "dump_score": 64.0}
    cont_low = _dump_continuation_short_ok(
        low_fuel, phase="dump_active", lc=lc_dump, fuel=64.0, cal_min_fuel=72.0
    )
    gate = evaluate_alert_gate(
        setup, direction="short", symbol="BEATUSDT", lifecycle=lc, row=row
    )
    tp2_block = _tp2_room_blocks(setup, price=9.0, min_room_pct=6.0, min_rr=1.15)
    gate_dump = evaluate_alert_gate(
        setup, direction="short", symbol="BEATUSDT", lifecycle=lc_dump, row=row
    )
    setup_adx = {**setup, "filter_blocks": ["adx1h_uptrend_56"]}
    gate_adx = evaluate_alert_gate(
        setup_adx, direction="short", symbol="BEATUSDT", lifecycle=lc, row=row
    )
    return [
        CaseResult("dump_continuation_impulse", cont, "structural+fall"),
        CaseResult(
            "dump_continuation_confirmed_low_fuel",
            cont_low,
            "confirmed+structural no fuel re-check",
        ),
        CaseResult(
            "dump_continuation_passes_gate",
            gate.ok or gate.code not in ("short_entry_not_ok", "tp2_too_close"),
            gate.code,
        ),
        CaseResult(
            "dump_confluence_single_struct",
            gate_dump.ok or gate_dump.code != "delivery_confluence_low",
            gate_dump.code,
        ),
        CaseResult("tp2_waiver_with_rr", not tp2_block, f"block={tp2_block}"),
        CaseResult(
            "impulse_short_adx_waiver",
            gate_adx.code != "filter_block",
            gate_adx.code,
        ),
        CaseResult(
            "dump_continuation_rr_110",
            evaluate_alert_gate(
                {
                    "confirmed": True,
                    "confirm_hard": ["5m_close_below_support"],
                    "levels_viable": True,
                    "risk_reward": 1.13,
                    "tp1": 8.5,
                    "tp2": 8.45,
                    "stop_loss": 9.2,
                },
                direction="short",
                symbol="SPACEUSDT",
                lifecycle={
                    "phase": "dump_active",
                    "fall_from_high_pct": 18.0,
                    "short_entry_ok": True,
                },
                row={
                    "price": 9.0,
                    "chg_24h_pct": 80.0,
                    "session": {"range_pct_24h": 50.0},
                },
            ).code
            not in ("rr_below_min", "tp2_too_close"),
            "rr1.13 dump leg",
        ),
    ]


def run_tracker_be_cases() -> list[CaseResult]:
    from hunt_watch.signal_tracker import apply_tp1_management

    active_long = {
        "entry_lo": 1.0,
        "entry_hi": 1.01,
        "stop_loss": 0.95,
        "tp1": 1.05,
    }
    apply_tp1_management(active_long, direction="long", symbol="BEATUSDT")
    be_long = float(active_long.get("stop_loss") or 0)
    # EPICUSDT post-mortem: 0.15% BE on memecoin → false stop after correct TP1.
    active_short = {
        "entry_lo": 0.546,
        "entry_hi": 0.561962,
        "stop_loss": 0.597132,
        "tp1": 0.511245,
    }
    apply_tp1_management(active_short, direction="short", symbol="EPICUSDT")
    be_short = float(active_short.get("stop_loss") or 0)
    short_buf_pct = (be_short / 0.561962 - 1.0) * 100.0
    return [
        CaseResult(
            "tp1_be_buffer_long",
            be_long < 1.0 and be_long > 0.98,
            f"stop={be_long}",
        ),
        CaseResult(
            "tp1_be_buffer_short_memecoin_min",
            short_buf_pct >= 1.0,
            f"stop={be_short} buf_pct={short_buf_pct:.2f}",
        ),
    ]


def run_tracker_outcome_cases() -> list[CaseResult]:
    from hunt_watch.tracker_outcomes import outcome_kind

    return [
        CaseResult(
            "bounce_invalidate_profit_is_win",
            outcome_kind("bounce_invalidate", pnl_pct=4.62) == "win",
            "FOLKS-style structural exit",
        ),
        CaseResult(
            "bounce_invalidate_loss_stays_loss",
            outcome_kind("bounce_invalidate", pnl_pct=-1.2) == "loss",
            "unprofitable bounce",
        ),
        CaseResult(
            "stop_hit_loss_ignores_small_profit_edge",
            outcome_kind("stop_hit", pnl_pct=-0.5) == "loss",
            "hard stop",
        ),
    ]


def run_stale_grace_cases() -> list[CaseResult]:
    """Near-TP1 stale grace: 8-tick hold when ≤3% remaining to TP1.

    Tests use tick counts that stay BELOW the close threshold so close_signal
    is never triggered — avoids writing TESTUSDT events to real signal_events.jsonl.
    """
    from datetime import UTC, datetime
    from hunt_watch.signal_tracker import _stale_lifecycle_invalidate, STALE_LC_TICKS_DEFAULT

    def _make_state(active: dict) -> dict:
        return {"signals": {"TESTUSDT:short": active}, "followup_sent": {}}

    ts = datetime.now(UTC)
    lc_stale = {"phase": "post_dump_bounce", "recommended_bias": "long"}

    # Case 1: far from TP1, tick just below default → still open (not closed yet)
    active_far_open = {
        "status": "active", "direction": "short",
        "entry_lo": 100.0, "entry_hi": 105.0, "tp1": 85.0,
        "extreme_lo": 99.0, "extreme_hi": 105.0,  # MFE ~5.5%, tp1_dist ~18.8% → far
        "stale_lc_ticks": STALE_LC_TICKS_DEFAULT - 2,  # one tick before threshold
    }
    state1 = _make_state(active_far_open)
    result1 = _stale_lifecycle_invalidate(
        state1, active_far_open, symbol="TESTUSDT", direction="short",
        lifecycle=lc_stale, row={}, price=99.0, ts=ts, announced=False, archive=False,
    )
    # tick becomes DEFAULT-1 → still below threshold, stays open
    far_still_open = result1 is None and state1["signals"]["TESTUSDT:short"].get("status") != "closed"

    # Case 2: near TP1 (≤3% remaining), tick=4 → grace=8, stays open
    active_near = {
        "status": "active", "direction": "short",
        "entry_lo": 100.0, "entry_hi": 105.0, "tp1": 84.0,
        "extreme_lo": 86.6, "extreme_hi": 105.0,  # MFE ~17.5%, tp1_dist ~19.9%, remaining ~2.9%
        "stale_lc_ticks": 4,
    }
    state2 = _make_state(active_near)
    result2 = _stale_lifecycle_invalidate(
        state2, active_near, symbol="TESTUSDT", direction="short",
        lifecycle=lc_stale, row={}, price=86.6, ts=ts, announced=False, archive=False,
    )
    near_tp1_grace_holds = result2 is None and state2["signals"]["TESTUSDT:short"].get("status") != "closed"

    # Case 3: far from TP1 at grace-level ticks → normal close (tick 4 > default 3)
    # Check via stale_lc_ticks increment only — don't trigger close
    active_far_ticks = {
        "status": "active", "direction": "short",
        "entry_lo": 100.0, "entry_hi": 105.0, "tp1": 85.0,
        "extreme_lo": 99.0, "extreme_hi": 105.0,
        "stale_lc_ticks": 1,  # will become 2 — still below 3
    }
    state3 = _make_state(active_far_ticks)
    _stale_lifecycle_invalidate(
        state3, active_far_ticks, symbol="TESTUSDT", direction="short",
        lifecycle=lc_stale, row={}, price=99.0, ts=ts, announced=False, archive=False,
    )
    far_ticks_incremented = active_far_ticks["stale_lc_ticks"] == 2

    return [
        CaseResult("stale_far_tp1_still_counting", far_still_open, "tick below default, stays open"),
        CaseResult("stale_near_tp1_grace_holds_at_tick4", near_tp1_grace_holds, "8-tick grace ≤3% from TP1"),
        CaseResult("stale_ticks_increment_correctly", far_ticks_incremented, f"stale_lc_ticks 1→2"),
    ]


def run_stale_entry_phase_cases() -> list[CaseResult]:
    """Same lifecycle phase as entry must not stale-close (SPACEUSDT class)."""
    from datetime import UTC, datetime

    from hunt_watch.signal_tracker import _stale_lifecycle_invalidate

    ts = datetime.now(UTC)
    lc = {"phase": "impulse_initiating", "recommended_bias": "long"}
    session = {"pos_in_range": 0.5}

    active_same = {
        "status": "active",
        "direction": "short",
        "entry_lifecycle_phase": "impulse_initiating",
        "entry_lo": 0.008763,
        "entry_hi": 0.009014,
        "tp1": 0.008499,
        "extreme_lo": 0.008253,
        "extreme_hi": 0.008368,
        "stale_lc_ticks": 2,
    }
    state1 = {"signals": {"SPACEUSDT:short": active_same}, "followup_sent": {}}
    r1 = _stale_lifecycle_invalidate(
        state1, active_same, symbol="SPACEUSDT", direction="short",
        lifecycle=lc, row={"session": session}, price=0.008368, ts=ts, announced=True, archive=False,
    )
    same_phase_holds = r1 is None and active_same.get("stale_lc_ticks") == 0

    active_tp1 = {
        **active_same,
        "stale_lc_ticks": 2,
        "tp1_hit": True,
        "tp1_managed": True,
        "entry_lifecycle_phase": "dump_active",
    }
    lc2 = {"phase": "impulse_initiating", "recommended_bias": "long"}
    state2 = {"signals": {"HUSDT:short": active_tp1}, "followup_sent": {}}
    r2 = _stale_lifecycle_invalidate(
        state2, active_tp1, symbol="HUSDT", direction="short",
        lifecycle=lc2, row={"session": session}, price=0.17, ts=ts, announced=True, archive=False,
    )
    tp1_managed_holds = r2 is None and active_tp1.get("status") != "closed"

    active_transition = {
        "status": "active",
        "direction": "short",
        "entry_lifecycle_phase": "dump_active",
        "entry_lo": 0.18,
        "entry_hi": 0.19,
        "tp1": 0.16,
        "extreme_lo": 0.17,
        "extreme_hi": 0.19,
        "stale_lc_ticks": 2,
    }
    state3 = {"signals": {"HUSDT:short": active_transition}, "followup_sent": {}}
    _stale_lifecycle_invalidate(
        state3, active_transition, symbol="HUSDT", direction="short",
        lifecycle=lc2, row={"session": session}, price=0.17, ts=ts, announced=False, archive=False,
    )
    transition_increments = active_transition.get("stale_lc_ticks") == 3

    return [
        CaseResult("stale_same_phase_no_close", same_phase_holds, "entry phase == lc phase"),
        CaseResult("stale_tp1_managed_holds", tp1_managed_holds, "no stale after TP1 mgmt"),
        CaseResult("stale_phase_transition_counts", transition_increments, "dump→impulse still stale"),
    ]


def run_feature_latch_cases() -> list[CaseResult]:
    """P0: feature vectors latched at open/peak/close + book_walls at entry."""
    from datetime import UTC, datetime

    from hunt_watch.feature_latch import book_walls_from_depth, feature_vector_from_row
    from hunt_watch.signal_tracker import close_signal, register_signal_open

    row_open = {
        "ts": "2026-06-11T12:00:00+00:00",
        "price": 1.25,
        "market": {"depth_imbalance": -0.12, "oi_z": 1.8},
        "regime": {"market_regime": "trend_down"},
        "lifecycle": {"phase": "dump_active", "recommended_bias": "short", "fall_from_high_pct": 18.0},
        "session": {"pos_in_range": 0.22},
        "book_walls": book_walls_from_depth(
            {
                "bid_price": 1.24,
                "ask_price": 1.26,
                "bid_levels": [{"price": 1.24, "qty": 1000.0, "notional_usd": 1240.0}],
                "ask_levels": [{"price": 1.26, "qty": 800.0, "notional_usd": 1008.0}],
            }
        ),
    }
    fv = feature_vector_from_row(row_open)
    walls_ok = isinstance(row_open.get("book_walls"), dict) and bool(row_open["book_walls"].get("bid_levels"))

    state: dict[str, Any] = {"signals": {}, "followup_sent": {}}
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    register_signal_open(
        state,
        symbol="LATCHUSDT",
        direction="short",
        price=1.25,
        setup={"entry_zone": [1.24, 1.25], "stop_loss": 1.30, "tp1": 1.10, "dump_score": 72},
        lifecycle={"phase": "dump_active", "recommended_bias": "short"},
        now=now,
        features_open=fv,
        book_walls=row_open["book_walls"],
    )
    active = state["signals"]["LATCHUSDT:short"]
    open_ok = isinstance(active.get("features_open"), dict) and isinstance(active.get("book_walls"), dict)

    active["extreme_lo"] = 1.15
    row_peak = {**row_open, "ts": "2026-06-11T13:00:00+00:00", "price": 1.15}
    from hunt_watch.signal_tracker import _tick_feature_latch

    _tick_feature_latch(active, row_peak, direction="short")
    peak_ok = isinstance(active.get("features_peak"), dict) and float(active.get("peak_mfe_pct") or 0) > 0

    close_signal(
        state,
        symbol="LATCHUSDT",
        direction="short",
        reason="tp1",
        exit_price=1.10,
        now=datetime(2026, 6, 11, 14, 0, tzinfo=UTC),
        archive=False,  # test must not pollute production signal_history.jsonl
    )
    closed = state["closed_history"][-1]
    close_ok = isinstance(closed.get("features_close"), dict) and "features_last" not in closed

    return [
        CaseResult("feature_vector_from_row", bool(fv.get("market")), "market dict captured"),
        CaseResult("book_walls_from_depth", walls_ok, "top bid/ask levels"),
        CaseResult("register_features_open", open_ok, "features_open + book_walls on open"),
        CaseResult("latch_features_peak", peak_ok, "features_peak on MFE improve"),
        CaseResult("latch_features_close", close_ok, "features_close on close, no features_last"),
    ]


def run_fast_flush_tp1_cases() -> list[CaseResult]:
    """ESPORTS post-mortem: single TP1 must be touched on violent first flush."""
    from hunt_watch.levels import fib_retracement_levels, structural_short_levels

    ih, il_stale, price = 0.27767, 0.0552, 0.26376
    atr15 = 0.012
    fib = fib_retracement_levels(ih, il_stale)
    old_tp1 = float(fib["ret_382"])
    flush_low = 0.19506

    lv = structural_short_levels(
        price=price,
        impulse_high=ih,
        impulse_low=il_stale,
        fib=fib,
        atr15=atr15,
        local_support=0.0,
        local_resistance=0.275,
        lifecycle_phase="exhaustion_at_high",
        fall_from_high_pct=5.0,
        range_pct_24h=220.0,
        leg_gain_pct=400.0,
        symbol="ESPORTSUSDT",
    )
    new_tp1 = float(lv["tp1"])
    hit_old = flush_low <= old_tp1
    hit_new = flush_low <= new_tp1

    return [
        CaseResult(
            "fast_flush_tp1_raised",
            new_tp1 > old_tp1,
            f"old={old_tp1:.6f} new={new_tp1:.6f}",
        ),
        CaseResult(
            "fast_flush_tp1_touched_on_esports_low",
            hit_new and not hit_old,
            f"low={flush_low} old_hit={hit_old} new_hit={hit_new}",
        ),
        CaseResult(
            "fast_flush_tp1_still_below_entry",
            new_tp1 < price,
            f"tp1={new_tp1} entry={price}",
        ),
    ]


def run_backtest_synthetic_cases() -> list[CaseResult]:
    from hunt_watch.backtest_synthetic import leg_events_to_signals, synthetic_levels
    from hunt_watch.pump_history import PumpHistoryStore, record_pump_leg
    from datetime import UTC, datetime

    short_lv = synthetic_levels("pump", 10.0, change_24h_pct=25.0)
    long_lv = synthetic_levels("dump", 1.0, change_24h_pct=-18.0)
    short_ok = (
        short_lv.get("direction") == "short"
        and float(short_lv["stop_loss"]) > 10.0
        and float(short_lv["tp1"]) < 10.0
    )
    long_ok = (
        long_lv.get("direction") == "long"
        and float(long_lv["stop_loss"]) < 1.0
        and float(long_lv["tp1"]) > 1.0
    )

    store = PumpHistoryStore()
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    record_pump_leg(store, symbol="SYNUSDT", kind="pump", source="ignition", price=2.5, now=now)
    record_pump_leg(store, symbol="SYNUSDT", kind="dump", source="lifecycle", price=2.2, now=now)
    signals = leg_events_to_signals(store, limit=10, dedupe_hours=1)
    has_short = any(s.get("leg_kind") == "pump" and s.get("direction") == "short" for s in signals)
    has_long = any(s.get("leg_kind") == "dump" and s.get("direction") == "long" for s in signals)

    return [
        CaseResult("synthetic_short_levels", short_ok, "pump→short SL above TP below"),
        CaseResult("synthetic_long_levels", long_ok, "dump→long SL below TP above"),
        CaseResult("leg_events_to_signals", len(signals) >= 2, f"n={len(signals)}"),
        CaseResult("leg_pump_maps_short", has_short, "leg_pump → short"),
        CaseResult("leg_dump_maps_long", has_long, "leg_dump → long"),
    ]


def run_dump_init_score_cases() -> list[CaseResult]:
    """Crowded-pump fade: MTF setup + 1m MACD trigger (ESPORTS post-mortem)."""
    from hunt_watch.dump_init_score import score_dump_init

    setup_tf = {
        "1m": {"closed_macd_hist": 0.00138, "macd_hist": 0.0015},
        "5m": {"closed_rsi14": 73.9, "closed_macd_hist": 0.0024},
        "15m": {"closed_rsi14": 86.0},
        "1h": {"closed_rsi14": 92.5},
    }
    setup_row = {
        "price": 0.25927,
        "dump": {},
        "lifecycle": {"phase": "exhaustion_at_high", "fall_from_high_pct": 1.1},
        "market": {"top_ls_1h": 1.99, "funding_pct": 0.629, "taker_5m": 0.997},
    }
    _, setup_reasons, setup_verdict = score_dump_init(
        row=setup_row, micro={}, tf=setup_tf, prev=None
    )

    trap_row = {
        **setup_row,
        "price": 0.27549,
        "lifecycle": {"phase": "exhaustion_at_high", "fall_from_high_pct": 0.0},
        "market": {**setup_row["market"], "taker_5m": 1.25},
    }
    trap_tf = {
        **setup_tf,
        "5m": {"closed_rsi14": 75.6, "closed_macd_hist": 0.0019},
        "1m": {"closed_macd_hist": 0.00154, "macd_hist": 0.0016},
    }
    trap_score, trap_reasons, trap_verdict = score_dump_init(
        row=trap_row, micro={}, tf=trap_tf, prev=None
    )

    prev_snap = {
        "price": 0.26544,
        "lifecycle": {"fall_from_high_pct": 3.8},
        "timeframes": {
            "1m": {"closed_macd_hist": 0.00008, "macd_hist": 0.0001},
        },
    }
    trigger_row = {
        "price": 0.26503,
        "dump": {"support_break_level": 0.277},
        "lifecycle": {"phase": "exhaustion_at_high", "fall_from_high_pct": 4.5},
        "market": {**setup_row["market"], "funding_pct": 0.78, "taker_5m": 1.164},
    }
    trigger_tf = {
        **setup_tf,
        "1m": {"closed_macd_hist": -0.00043, "macd_hist": -0.0004},
        "5m": {"closed_rsi14": 80.8, "closed_macd_hist": 0.0023},
        "1h": {"closed_rsi14": 93.2},
    }
    trig_score, trig_reasons, trig_verdict = score_dump_init(
        row=trigger_row, micro={}, tf=trigger_tf, prev=prev_snap
    )

    return [
        CaseResult(
            "dump_init_setup_watch",
            setup_verdict in ("DUMP_WATCH", "DUMP_ARMED") and "1h_rsi=" in str(setup_reasons),
            f"verdict={setup_verdict} reasons={setup_reasons[:4]}",
        ),
        CaseResult(
            "dump_init_squeeze_not_likely",
            trap_verdict == "DUMP_WATCH" and trap_score < 85,
            f"verdict={trap_verdict} score={trap_score} trap={trap_reasons}",
        ),
        CaseResult(
            "dump_init_1m_macd_trigger",
            "1m_macd_cross_down" in trig_reasons or "1m_macd_hist_neg" in trig_reasons,
            f"reasons={trig_reasons}",
        ),
        CaseResult(
            "dump_init_trigger_armed",
            trig_verdict in ("DUMP_ARMED", "DUMP_LIKELY") and trig_score >= 70,
            f"verdict={trig_verdict} score={trig_score}",
        ),
    ]


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    failed = [r for r in results if not r.ok]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "failures": [{"name": r.name, "detail": r.detail} for r in failed],
        "cases": [{"name": r.name, "ok": r.ok, "detail": r.detail} for r in results],
    }

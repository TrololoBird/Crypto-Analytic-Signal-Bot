#!/usr/bin/env python3
"""Independent critical audit — recompute hunt triggers from live REST, distrust bot output."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.lifecycle import HuntPhase, assess_hunt_lifecycle, effective_support_break
from hunt_watch.market_regime import active_params
from hunt_watch.signal_engine import cluster_fuel, confirm_dump, confirm_long


async def _bot_row(symbol: str) -> dict[str, Any]:
    from hunt_watch.symbol_probe import probe_symbol_signal

    return await probe_symbol_signal(symbol, stagger_ms=120, auto_watchlist=False)


def _indie_lifecycle_inputs(row: dict[str, Any]) -> dict[str, Any]:
    s = row.get("session") or {}
    imp = row.get("impulse") or {}
    px = float(row.get("price") or 0)
    hh, hl = float(imp.get("hunt_high") or 0), float(imp.get("hunt_low") or 0)
    lo = float(s.get("low_24h") or 0)
    fall = (hh - px) / hh * 100 if hh else 0
    rally = (px - lo) / lo * 100 if lo else 0
    leg = (hh - hl) / hl * 100 if hl else 0
    return {
        "fall_from_high_pct": round(fall, 2),
        "rally_from_24h_low_pct": round(rally, 2),
        "leg_gain_pct": round(leg, 1),
        "upward_leg_shallow_pullback": leg >= 20.0 and fall < 8.0,
        "meaningful_dump": fall >= 8.0,
        "pos_in_range": s.get("pos_in_range"),
    }


def _indie_lifecycle(row: dict[str, Any]) -> dict[str, Any]:
    imp = row.get("impulse") or {}
    lc = assess_hunt_lifecycle(
        price=float(row.get("price") or 0),
        hunt_high=float(imp.get("hunt_high") or 0),
        hunt_low=float(imp.get("hunt_low") or 0),
        session=row.get("session") or {},
        tf=row.get("timeframes") or {},
        market=row.get("market") or {},
    )
    return {
        "phase": lc.phase.value,
        "bias": lc.recommended_bias,
        "reasons": list(lc.reasons),
        "fall": lc.fall_from_high_pct,
        "bounce": lc.bounce_from_low_pct,
        "short_entry_ok": lc.short_entry_ok,
        "short_confirm_ok": lc.short_confirm_ok,
    }


def _indie_dump_triggers(row: dict[str, Any]) -> dict[str, Any]:
    """Re-derive short score triggers from row timeframes (no bot _dump_analysis)."""
    tf = row.get("timeframes") or {}
    mkt = row.get("market") or {}
    imp = row.get("impulse") or {}
    dump = row.get("dump") or {}
    px = float(row.get("price") or 0)
    r15 = tf.get("15m_closed") or tf.get("15m") or {}
    r1h = tf.get("1h") or {}
    r4h = tf.get("4h") or {}
    r5c = tf.get("5m_closed") or {}
    fund = mkt.get("funding_pct")
    taker = mkt.get("taker_5m") or mkt.get("taker_1h")
    hits: list[str] = []
    misses: list[str] = []
    if float(r15.get("rsi14") or 0) >= 72:
        hits.append("rsi15_overbought")
    else:
        misses.append(f"rsi15={r15.get('rsi14')}")
    if float(r1h.get("rsi14") or 0) >= 72:
        hits.append("rsi1h_overbought")
    else:
        misses.append(f"rsi1h={r1h.get('rsi14')}")
    if r4h.get("bearish_rsi_div"):
        hits.append("bear_div_4h")
    if r1h.get("bearish_rsi_div"):
        hits.append("bear_div_1h")
    if fund is not None and float(fund) > 0.05:
        hits.append("crowded_long_funding")
    if taker is not None and float(taker) < 0.98:
        hits.append("taker_sell_pressure")
    elif taker is not None and float(taker) > 1.05:
        misses.append(f"taker_buy_{taker} (no sell pressure)")
    support = float(dump.get("support_break_level") or 0)
    r5_close = float(r5c.get("close") or 0)
    if support and r5_close < support:
        hits.append(f"lost_support_{support}")
    elif float(imp.get("hunt_high") or 0) and r5_close < float(imp["hunt_high"]) * 0.998:
        hits.append("below_impulse_high")
    return {"expected_subset": hits, "not_expected": misses}


def _indie_confirm(row: dict[str, Any], *, direction: str) -> dict[str, Any]:
    cal = active_params()
    tf = row.get("timeframes") or {}
    lc = row.get("lifecycle") or {}
    bias = str(lc.get("recommended_bias") or "")
    if direction == "short":
        setup = row.get("dump") or {}
        conf, hard = confirm_dump(
            setup,
            tf,
            symbol=str(row.get("symbol") or ""),
            price=float(row.get("price") or 0),
            market=row.get("market") or {},
            cal=cal,
            lifecycle_bias=bias if bias in {"long", "short", "wait"} else "",
        )
    else:
        setup = row.get("long") or {}
        conf, hard = confirm_long(setup, tf, cal=cal, lifecycle_bias=bias)
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    triggers = list(setup.get("triggers") or [])
    raw = float(setup.get("dump_score" if direction == "short" else "long_score") or 0)
    fuel_re = cluster_fuel(triggers, raw_score=raw)
    return {
        "confirmed": conf,
        "hard": hard,
        "fuel_recomputed": fuel_re,
        "fuel_bot": setup.get(fuel_key),
    }


def _indie_support(row: dict[str, Any]) -> dict[str, Any]:
    imp = row.get("impulse") or {}
    lc_raw = row.get("lifecycle") or {}
    from hunt_watch.lifecycle import HuntLifecycle

    lc = HuntLifecycle(
        phase=HuntPhase(str(lc_raw.get("phase") or "no_setup")),
        recommended_bias=lc_raw.get("recommended_bias") or "wait",
        short_entry_ok=bool(lc_raw.get("short_entry_ok")),
        short_confirm_ok=bool(lc_raw.get("short_confirm_ok")),
        invalidate_short=bool(lc_raw.get("invalidate_short")),
        fall_from_high_pct=float(lc_raw.get("fall_from_high_pct") or 0),
        bounce_from_low_pct=float(lc_raw.get("bounce_from_low_pct") or 0),
        local_support=float(lc_raw.get("local_support") or 0),
        local_resistance=float(lc_raw.get("local_resistance") or 0),
        reasons=tuple(lc_raw.get("reasons") or ()),
    )
    pos = float((row.get("session") or {}).get("pos_in_range") or 0.5)
    support = effective_support_break(
        impulse_high=float(imp.get("hunt_high") or 0),
        lifecycle=lc,
        pos_in_range=pos,
    )
    tf = row.get("timeframes") or {}
    r5 = float((tf.get("5m_closed") or {}).get("close") or 0)
    r15 = float((tf.get("15m_closed") or {}).get("close") or 0)
    return {
        "support_level": support,
        "r5_closed": r5,
        "r15_closed": r15,
        "r5_below": r5 < support if support else None,
        "r15_below": r15 < support if support else None,
        "bot_support": (row.get("dump") or {}).get("support_break_level"),
    }


def _compare(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    checks: list[str] = []
    lc_bot = row.get("lifecycle") or {}
    lc_ind = _indie_lifecycle(row)
    if lc_bot.get("phase") != lc_ind["phase"]:
        issues.append(f"lifecycle_phase bot={lc_bot.get('phase')} indie={lc_ind['phase']}")
    else:
        checks.append(f"lifecycle_ok={lc_ind['phase']}")

    inputs = _indie_lifecycle_inputs(row)
    dump = row.get("dump") or {}
    indie_conf = _indie_confirm(row, direction="short")
    if bool(dump.get("confirmed")) != indie_conf["confirmed"]:
        issues.append(
            f"dump_confirmed bot={dump.get('confirmed')} indie={indie_conf['confirmed']} "
            f"hard={indie_conf['hard']}"
        )
    else:
        checks.append(f"dump_confirm_ok={indie_conf['confirmed']}")

    if abs(float(dump.get("dump_fuel") or 0) - float(indie_conf["fuel_recomputed"])) > 0.6:
        issues.append(
            f"dump_fuel bot={dump.get('dump_fuel')} recomputed={indie_conf['fuel_recomputed']}"
        )

    sup = _indie_support(row)
    if sup["bot_support"] and abs(float(sup["bot_support"]) - float(sup["support_level"])) > 0.0001:
        issues.append(f"support bot={sup['bot_support']} indie={sup['support_level']}")

    trig = _indie_dump_triggers(row)
    bot_trigs = set(str(t) for t in (dump.get("triggers") or []))
    for t in trig["expected_subset"]:
        if not any(t.split("_")[0] in bt or t in bt for bt in bot_trigs):
            issues.append(f"missing_trigger expected~{t}")

    # Logic smell checks (design, not code path)
    if inputs["upward_leg_shallow_pullback"] and lc_bot.get("phase") == "post_dump_bounce":
        issues.append("DESIGN_BUG: post_dump_bounce on upward leg without meaningful dump")
    if float(dump.get("dump_fuel") or 0) >= cal_confirm() and indie_conf["confirmed"]:
        hard = indie_conf["hard"]
        if not any("close_below_support" in h for h in hard) and len(hard) < 2:
            issues.append(f"DESIGN: confirm with weak hard={hard}")
    taker = (row.get("market") or {}).get("taker_5m")
    if taker and float(taker) > 1.1 and "taker_sell_pressure" in bot_trigs:
        issues.append(f"DESIGN_BUG: taker_sell with taker_buy={taker}")

    squeeze = row.get("squeeze")
    bb = (row.get("timeframes") or {}).get("1h", {}).get("bb_width_pctile")
    if squeeze and bb and float(bb) > 0.5:
        issues.append(f"DESIGN_BUG: squeeze alert with bb_pctile={bb} (expansion not squeeze)")

    return {
        "symbol": symbol,
        "ts": row.get("ts"),
        "ok": not issues,
        "issues": issues,
        "checks": checks,
        "lifecycle_inputs": inputs,
        "lifecycle_indie": lc_ind,
        "lifecycle_bot": {
            "phase": lc_bot.get("phase"),
            "bias": lc_bot.get("recommended_bias"),
            "reasons": lc_bot.get("reasons"),
        },
        "confirm_short": indie_conf,
        "support": sup,
        "trigger_audit": trig,
        "dump_bot": {
            "fuel": dump.get("dump_fuel"),
            "score": dump.get("dump_score"),
            "phase": dump.get("phase"),
            "confirmed": dump.get("confirmed"),
            "hard": dump.get("confirm_hard"),
            "triggers": dump.get("triggers"),
            "filter_blocks": dump.get("filter_blocks"),
            "levels_viable": dump.get("levels_viable"),
        },
        "alert_would_send": _alert_sim(row),
    }


def cal_confirm() -> float:
    return active_params().confirm_min_score


def _alert_sim(row: dict[str, Any]) -> dict[str, Any]:
    """Mirror _should_alert gates without importing watch (duplicate thresholds)."""
    dump = row.get("dump") or {}
    lc = row.get("lifecycle") or {}
    sess = row.get("session") or {}
    cal = active_params()
    reasons: list[str] = []
    if not dump.get("confirmed"):
        reasons.append("not_confirmed")
    fuel = float(dump.get("dump_fuel") or 0)
    if fuel < cal.forming_min_score:
        reasons.append("below_forming_min")
    if dump.get("filter_blocks"):
        reasons.append(f"filter_block:{dump['filter_blocks']}")
    chg = abs(float(row.get("chg_24h_pct") or 0))
    rng = float(sess.get("range_pct_24h") or 0)
    if not (row.get("young_listing") or chg >= cal.anomaly_min_chg_24h_pct or rng >= cal.anomaly_min_range_24h_pct):
        reasons.append(f"not_anomaly chg={chg} rng={rng}")
    if not lc.get("short_entry_ok"):
        reasons.append("short_entry_not_ok")
    rr = dump.get("risk_reward")
    if rr is not None and float(rr) < cal.min_risk_reward:
        reasons.append(f"rr_below_{cal.min_risk_reward}")
    return {"would_alert": not reasons, "blockers": reasons}


async def _main_async(symbols: list[str], *, source: str = "cli") -> int:
    from hunt_watch.signal_audit import audit_probe_row

    report: list[dict[str, Any]] = []
    for sym in symbols:
        print(f"auditing {sym} …", file=sys.stderr, flush=True)
        row = await _bot_row(sym)
        if row.get("error"):
            report.append({"symbol": sym, "ok": False, "issues": [row["error"]]})
            continue
        cmp = _compare(sym, row)
        cmp["signal_audit"] = audit_probe_row(row, source=source)
        report.append(cmp)
    out = {"ts": datetime.now(UTC).isoformat(), "reports": report}
    print(json.dumps(out, indent=2, default=str))
    bad = [
        r for r in report
        if not r.get("ok") or not (r.get("signal_audit") or {}).get("ok", True)
    ]
    return 1 if bad else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Critical independent hunt audit")
    parser.add_argument("symbols", nargs="*", help="e.g. BEATUSDT")
    parser.add_argument(
        "--pending",
        action="store_true",
        help="Audit all symbols in signal_notify.json pending queue",
    )
    args = parser.parse_args()
    syms: list[str] = [s.upper() for s in args.symbols]
    if args.pending:
        from hunt_watch.signal_audit import load_pending_symbols

        syms = load_pending_symbols()
        if not syms:
            print(json.dumps({"note": "no pending signals"}))
            raise SystemExit(0)
    if not syms:
        parser.error("provide symbols or --pending")
    raise SystemExit(asyncio.run(_main_async(syms, source="pending" if args.pending else "cli")))


if __name__ == "__main__":
    main()

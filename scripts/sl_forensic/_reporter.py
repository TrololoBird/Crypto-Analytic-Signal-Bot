"""Markdown report generation for SL forensic cases."""

from __future__ import annotations

from collections import Counter
from typing import Any


def _score_band(score: float | None) -> str:
    if score is None:
        return "N/A"
    if score >= 0.65:
        return "HIGH"
    if score >= 0.55:
        return "MED"
    return "LOW"


def _rr_band(rr: float | None) -> str:
    if rr is None:
        return "N/A"
    if rr >= 2.0:
        return "GOOD"
    if rr >= 1.5:
        return "OK"
    return "BAD"


def _vol_band(atr_pct: float | None) -> str:
    if atr_pct is None:
        return "unknown"
    if atr_pct >= 2.0:
        return "high vol"
    if atr_pct >= 1.0:
        return "moderate"
    return "low vol"


def _fix_recommendation(case: dict[str, Any]) -> str:
    sl_type = str(case.get("sl_type") or "")
    subtype = str(case.get("sl_subtype") or "")
    setup_id = str(case.get("setup_id") or "")
    symbol = str(case.get("symbol") or "")
    direction = str(case.get("direction") or "")
    btc_bias = str(case.get("btc_bias") or "unknown")
    btc_move = float(case.get("btc_move_in_sl_candle_pct") or 0.0)
    deviation = float(case.get("entry_deviation_atr_mult") or 0.0)

    if sl_type == "STOP_HUNT":
        return (
            f"SL was placed at a liquidity sweep zone. Options:\n"
            f"   (1) Widen ATR multiplier for {setup_id} by 1.3× in "
            f"config/strategies/{setup_id}.toml\n"
            f"   (2) Use post-wick entry: delay entry by 1 candle after pattern fires\n"
            f"   (3) Place SL below the full wick low, not ATR-based"
        )
    if sl_type == "IMMEDIATE_ADVERSE" and subtype == "BTC_DRAG":
        return (
            f"BTC moved {btc_move:.2f}% in the SL candle, dragging {symbol}.\n"
            f"   Options:\n"
            f"   (1) Add BTC momentum filter: reject signal if BTC momentum opposes direction\n"
            f"   (2) Reduce position TTL during high-volatility BTC periods"
        )
    if sl_type == "IMMEDIATE_ADVERSE" and subtype == "ENTRY_CHASE":
        return (
            f"Price had moved {deviation:.2f}×ATR from entry by activation time.\n"
            f"   fix-sl-A (entry_staleness filter) should catch this in future runs.\n"
            f"   Verify entry_staleness filter is active and max_entry_deviation_atr_mult=1.5"
        )
    if sl_type == "IMMEDIATE_ADVERSE" and subtype == "FALSE_SIGNAL":
        return (
            f"Strategy detector fires on real-time data but NOT on confirmed historical data.\n"
            f"   This is a closed-candle confirmation bug in {setup_id}.\n"
            f"   The df[-2] fix should be applied to this strategy."
        )
    if sl_type == "THESIS_FAILED" and subtype == "WRONG_DIRECTION":
        return (
            f"Signal direction ({direction}) was AGAINST confirmed BTC bias ({btc_bias}).\n"
            f"   Regime direction filter (fix-sl-C) would block this signal.\n"
            f"   Consider enabling: filters.regime_filter_enabled = true in config.toml"
        )
    if sl_type == "TIMING_OFF":
        return (
            "Thesis was directionally correct but SL/TTL too tight.\n"
            f"   Consider widening SL ATR multiplier or extending TTL for {setup_id}."
        )
    return "Review setup parameters and market context filters for this pattern."


def generate_case_card(case: dict[str, Any]) -> str:
    """Generate Markdown card for one SL case."""
    score = case.get("score")
    atr_pct = case.get("atr_pct")
    rr = case.get("rr_ratio")
    deviation = case.get("entry_deviation_atr_mult")
    confirmed = case.get("entry_candle_was_confirmed")
    tp1_reached = bool(case.get("post_sl_tp1_reached"))
    tp1_candles = case.get("post_sl_tp1_candles")
    recheck = case.get("strategy_recheck_valid")
    recheck_reason = case.get("strategy_recheck_reason") or ""

    if recheck is True:
        recheck_label = "YES"
    elif recheck is False:
        recheck_label = "NO"
    else:
        recheck_label = "N/A"

    tp1_label = (
        f"YES in {tp1_candles} candles"
        if tp1_reached and tp1_candles is not None
        else "NO"
    )

    lines = [
        f"## Case: {case.get('setup_id')} {case.get('direction')} "
        f"{case.get('symbol')} @ {case.get('signal_created_at')}",
        "",
        f"**Verdict:** {case.get('sl_type')} / {case.get('sl_subtype')}",
        f"> {case.get('sl_verdict')}",
        "",
        "### Timeline",
        "| Event | Time | Price |",
        "|-------|------|-------|",
        f"| Signal created | {case.get('signal_created_at')} | {case.get('entry_price')} |",
        f"| Position activated | {case.get('entry_activated_at')} | {case.get('entry_price')} |",
        f"| SL hit | {case.get('sl_hit_at')} | {case.get('sl_price')} |",
        f"| Time to entry | {case.get('time_to_entry_min')} min | — |",
        f"| Time to SL | {case.get('time_to_sl_min')} min | — |",
        "",
        "### Setup quality",
        "| Metric | Value | Assessment |",
        "|--------|-------|------------|",
        f"| Score | {score} | {_score_band(float(score) if score is not None else None)} |",
        f"| ATR% | {atr_pct} | {_vol_band(float(atr_pct) if atr_pct is not None else None)} |",
        f"| R:R | {rr} | {_rr_band(float(rr) if rr is not None else None)} |",
        f"| Entry deviation | {deviation}×ATR | "
        f"{'STALE' if deviation and float(deviation) > 1.0 else 'FRESH'} |",
        f"| Confirmed candle | {confirmed} | {'YES' if confirmed else 'NO'} |",
        "",
        "### Market context at signal",
        "| BTC bias | Market regime | Direction vs bias |",
        "|----------|---------------|-------------------|",
        f"| {case.get('btc_bias')} | {case.get('market_regime')} | "
        f"{case.get('direction_vs_bias')} |",
        "",
        "### Post-SL price action",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Max recovery after SL | {float(case.get('post_sl_max_recovery_pct') or 0):.2f}% |",
        f"| Max adverse after SL | {float(case.get('post_sl_max_adverse_pct') or 0):.2f}% |",
        f"| TP1 reached after SL? | {tp1_label} |",
        "",
        "### BTC correlation",
        "| BTC move in SL candle | Direction match | BTC caused SL? |",
        "|-----------------------|-----------------|----------------|",
        f"| {float(case.get('btc_move_in_sl_candle_pct') or 0):.2f}% | "
        f"{case.get('btc_direction_match')} | "
        f"{'YES' if case.get('btc_caused_sl') else 'NO'} |",
        "",
        "### Strategy recheck",
        f"**Would detector fire on confirmed historical data?** {recheck_label}",
    ]
    if recheck_label == "NO" and recheck_reason:
        lines.append(recheck_reason)
    lines.extend(
        [
            "",
            "### Fix recommendation",
            _fix_recommendation(case),
            "",
            "---",
            "",
        ]
    )
    return "\n".join(lines)


def generate_aggregate_report(cases: list[dict[str, Any]]) -> str:
    """Generate aggregate REPORT_SL_FORENSIC.md content."""
    if not cases:
        return "# SL Forensic Report\n\nNo SL cases analyzed.\n"

    type_counts = Counter(str(c.get("sl_type") or "UNKNOWN") for c in cases)
    subtype_counts = Counter(
        f"{c.get('sl_type')}/{c.get('sl_subtype')}" for c in cases
    )
    setup_counts: dict[str, Counter[str]] = {}
    for case in cases:
        setup = str(case.get("setup_id") or "unknown")
        setup_counts.setdefault(setup, Counter())[str(case.get("sl_type") or "UNKNOWN")] += 1

    total = len(cases)
    lines = [
        "# SL Forensic Report",
        "",
        f"**Cases analyzed:** {total}",
        "",
        "## Executive summary",
        "",
        "| SL Type | Count | % |",
        "|---------|------:|--:|",
    ]
    for sl_type in ("STOP_HUNT", "IMMEDIATE_ADVERSE", "THESIS_FAILED", "TIMING_OFF"):
        count = type_counts.get(sl_type, 0)
        pct = count / total * 100.0 if total else 0.0
        lines.append(f"| {sl_type} | {count} | {pct:.1f}% |")

    lines.extend(["", "### Subtypes", ""])
    for key, count in subtype_counts.most_common():
        lines.append(f"- {key}: {count}")

    lines.extend(["", "## Per-strategy breakdown", ""])
    for setup, counter in sorted(setup_counts.items()):
        parts = ", ".join(f"{k}={v}" for k, v in counter.items())
        lines.append(f"- **{setup}**: {parts}")

    lines.extend(["", "## Actionable recommendations by type", ""])
    rec_by_type: dict[str, list[str]] = {}
    for case in cases:
        sl_type = str(case.get("sl_type") or "")
        rec = _fix_recommendation(case)
        if rec not in rec_by_type.setdefault(sl_type, []):
            rec_by_type[sl_type].append(rec)
    for sl_type, recs in rec_by_type.items():
        lines.append(f"### {sl_type}")
        for rec in recs[:2]:
            lines.append(rec)
            lines.append("")

    urgent = [
        c
        for c in cases
        if c.get("sl_type") in {"STOP_HUNT", "IMMEDIATE_ADVERSE"}
        and c.get("sl_subtype") in {"ENTRY_CHASE", "FAST_RECOVERY", "SLOW_RECOVERY", "BTC_DRAG"}
    ]
    lines.extend(["", "## Cases requiring immediate fix", ""])
    if urgent:
        for case in urgent:
            lines.append(
                f"- {case.get('symbol')} {case.get('setup_id')}: "
                f"{case.get('sl_type')}/{case.get('sl_subtype')}"
            )
    else:
        lines.append("None flagged.")

    lines.extend(["", "## known-gaps", ""])
    lines.append(
        "**G1 (TP1 outcomes):** `_mark_tp1()` updates `active_signals` only; "
        "`signal_outcomes.result` is written once on close via `_close_event`. "
        "Signals that touch TP1 then close as `breakeven_stop` or `expired_active` "
        "never appear as `tp1_hit` in `signal_outcomes`. Fix belongs in "
        "`create_outcome_from_tracked()` outcome remapping, not a missing await."
    )

    lines.extend(["", "## Full case cards", ""])
    for case in cases:
        lines.append(generate_case_card(case))

    return "\n".join(lines)

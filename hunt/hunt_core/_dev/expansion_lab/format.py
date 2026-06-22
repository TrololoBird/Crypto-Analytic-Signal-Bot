"""Telegram / HTML formatting for the Expansion Engine.

Renders the PRE-PUMP / PRE-DUMP opportunity card and a compact deep-report section.
This is the Expansion product's own surface — kept visually separate from Verdict V2.
"""
from __future__ import annotations

import html
from typing import Any

from hunt_core._dev.expansion_lab.types import ExpansionOpportunity

_STATE_HEADER = {
    "pre_pump": "🟢 PRE-PUMP DETECTED",
    "pre_dump": "🔴 PRE-DUMP DETECTED",
    "active_pump": "🟢 ACTIVE PUMP",
    "active_dump": "🔴 ACTIVE DUMP",
    "accumulation": "🟡 ACCUMULATION",
    "distribution": "🟠 DISTRIBUTION",
    "neutral": "⚪ NEUTRAL",
}


def _fmt_price(v: float) -> str:
    try:
        from hunt_core.deliver._labels import fmt_price

        return fmt_price(v)
    except Exception:
        if v >= 100:
            return f"{v:,.2f}"
        if v >= 1:
            return f"{v:.4f}"
        return f"{v:.6f}"


def format_expansion_card(opp: ExpansionOpportunity | dict[str, Any]) -> str:
    if isinstance(opp, dict):
        return _format_expansion_card_dict(opp)
    return _format_expansion_card_obj(opp)


def _format_expansion_card_obj(opp: ExpansionOpportunity) -> str:
    sym = html.escape(opp.symbol.replace("USDT", "-USDT"))
    p = opp.probabilities
    lines = [
        _STATE_HEADER.get(opp.state, "⚪ EXPANSION"),
        "",
        f"Coin: <code>{sym}</code>",
        "",
        f"Opportunity Score: <b>{opp.meta.opportunity_score:.2f}</b>",
        f"Expansion Quality: {opp.meta.expansion_quality:.2f}",
        f"Trigger Probability: {opp.trigger_probability:.2f}",
        f"Readiness: <b>{opp.readiness.upper()}</b>",
        f"Fake Breakout Risk: {opp.meta.fake_breakout_risk:.2f}",
        f"P(Up) {p.p_up:.2f} · P(Down) {p.p_down:.2f} · P(None) {p.p_none:.2f}",
        "",
        f"Stage: {html.escape(opp.stage)} ({opp.lifecycle_stage}/6)",
    ]

    if opp.forecast is not None:
        mv = opp.forecast.expected_move_pct
        hz = opp.forecast.expected_horizon_h
        lines += [
            "",
            f"Expected Move: {mv[0]:+.0f}% → {mv[1]:+.0f}%",
            f"Expected Horizon: {hz[0]:.0f}–{hz[1]:.0f}h",
        ]

    if opp.execution is not None:
        ex = opp.execution
        eb = ex.entry_band
        tgts = " / ".join(_fmt_price(t) for t in ex.targets)
        lines += [
            "",
            f"Activation: <code>{_fmt_price(ex.activation)}</code>",
            f"Entry Zone: <code>{_fmt_price(eb[0])}–{_fmt_price(eb[1])}</code>",
            f"Stop: <code>{_fmt_price(ex.stop)}</code>",
            f"Targets: <code>{tgts}</code>",
        ]

    drivers = opp.main_drivers or opp.evidence
    if drivers:
        lines.append("")
        lines.append("Main Drivers:")
        for d in drivers[:4]:
            lines.append(f"• {html.escape(str(d))}")

    lines += [
        "",
        f"Coverage: market-data only · Risk: {opp.risk.capitalize()}",
        "<i>Expansion Engine — energy + fuel + trigger · not a pump oracle · not financial advice</i>",
    ]
    return "\n".join(lines)


def _format_expansion_card_dict(d: dict[str, Any]) -> str:
    state = str(d.get("state") or "neutral")
    sym = html.escape(str(d.get("symbol") or "?").replace("USDT", "-USDT"))
    meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
    probs = d.get("probabilities") if isinstance(d.get("probabilities"), dict) else {}
    lines = [
        _STATE_HEADER.get(state, "⚪ EXPANSION"),
        "",
        f"Coin: <code>{sym}</code>",
        "",
        f"Opportunity Score: <b>{float(meta.get('opportunity_score') or 0):.2f}</b>",
        f"Expansion Quality: {float(meta.get('expansion_quality') or 0):.2f}",
        f"Trigger Probability: {float(d.get('trigger_probability') or 0):.2f}",
        f"Readiness: <b>{str(d.get('readiness') or 'low').upper()}</b>",
        f"Fake Breakout Risk: {float(meta.get('fake_breakout_risk') or 0):.2f}",
        f"P(Up) {float(probs.get('p_up') or 0):.2f} · "
        f"P(Down) {float(probs.get('p_down') or 0):.2f} · "
        f"P(None) {float(probs.get('p_none') or 0):.2f}",
        "",
        f"Stage: {html.escape(str(d.get('stage') or ''))} ({int(d.get('lifecycle_stage') or 0)}/6)",
    ]

    fc = d.get("forecast") if isinstance(d.get("forecast"), dict) else None
    if fc and fc.get("expected_move_pct"):
        mv = fc["expected_move_pct"]
        hz = fc.get("expected_horizon_h") or (0, 0)
        lines += [
            "",
            f"Expected Move: {float(mv[0]):+.0f}% → {float(mv[1]):+.0f}%",
            f"Expected Horizon: {float(hz[0]):.0f}–{float(hz[1]):.0f}h",
        ]

    ex = d.get("execution") if isinstance(d.get("execution"), dict) else None
    if ex:
        eb = ex.get("entry_band") or (0, 0)
        targets = ex.get("targets") or []
        tgts = " / ".join(_fmt_price(float(t)) for t in targets)
        lines += [
            "",
            f"Activation: <code>{_fmt_price(float(ex.get('activation') or 0))}</code>",
            f"Entry Zone: <code>{_fmt_price(float(eb[0]))}–{_fmt_price(float(eb[1]))}</code>",
            f"Stop: <code>{_fmt_price(float(ex.get('stop') or 0))}</code>",
            f"Targets: <code>{tgts}</code>",
        ]

    drivers = d.get("main_drivers") or d.get("evidence") or []
    if drivers:
        lines.append("")
        lines.append("Main Drivers:")
        for item in list(drivers)[:4]:
            lines.append(f"• {html.escape(str(item))}")

    risk = str(d.get("risk") or "medium").capitalize()
    lines += [
        "",
        f"Coverage: market-data only · Risk: {risk}",
        "<i>Expansion Engine — energy + fuel + trigger · not a pump oracle · not financial advice</i>",
    ]
    return "\n".join(lines)


def format_expansion_section(opp: ExpansionOpportunity) -> str:
    """Compact block for the deep report (kept separate from the Verdict block)."""
    if opp.state == "neutral" and opp.expansion_score < 0.45:
        return ""
    p = opp.probabilities
    head = _STATE_HEADER.get(opp.state, "⚪ EXPANSION").split(" ", 1)[-1]
    lines = [
        f"🧨 <b>Expansion Opportunity</b> — {html.escape(head)}",
        f"  quality {opp.meta.expansion_quality:.2f} · trigger {opp.trigger_probability:.2f} · "
        f"opp {opp.meta.opportunity_score:.2f}",
        f"  P(up) {p.p_up:.2f} · P(down) {p.p_down:.2f} · P(none) {p.p_none:.2f} · "
        f"stage {opp.lifecycle_stage}/6 ({html.escape(opp.stage)})",
    ]
    if opp.forecast is not None:
        mv = opp.forecast.expected_move_pct
        lines.append(f"  expected {mv[0]:+.0f}%→{mv[1]:+.0f}% · readiness {opp.readiness}")
    if opp.main_drivers:
        lines.append("  drivers: " + ", ".join(html.escape(str(d)) for d in opp.main_drivers[:3]))
    return "\n".join(lines)


def format_expansion_section_from_dict(d: dict[str, Any]) -> str:
    """Deep-report section rendered directly from the stamped ``row["expansion"]`` dict.

    Avoids rebuilding the opportunity object; used by the deep Telegram formatter so the
    Expansion block sits beside (never merged with) the Verdict block.
    """
    if not isinstance(d, dict) or not d:
        return ""
    state = str(d.get("state") or "neutral")
    score = float(d.get("expansion_score") or 0.0)
    if state == "neutral" and score < 0.45:
        return ""
    meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
    probs = d.get("probabilities") if isinstance(d.get("probabilities"), dict) else {}
    head = _STATE_HEADER.get(state, "⚪ EXPANSION").split(" ", 1)[-1]
    lines = [
        f"🧨 <b>Expansion Opportunity</b> — {html.escape(head)}",
        f"  quality {float(meta.get('expansion_quality') or 0):.2f} · "
        f"trigger {float(d.get('trigger_probability') or 0):.2f} · "
        f"opp {float(meta.get('opportunity_score') or 0):.2f}",
        f"  P(up) {float(probs.get('p_up') or 0):.2f} · P(down) {float(probs.get('p_down') or 0):.2f} · "
        f"P(none) {float(probs.get('p_none') or 0):.2f} · "
        f"stage {int(d.get('lifecycle_stage') or 0)}/6 ({html.escape(str(d.get('stage') or ''))})",
    ]
    fc = d.get("forecast") if isinstance(d.get("forecast"), dict) else None
    if fc and fc.get("expected_move_pct"):
        mv = fc["expected_move_pct"]
        lines.append(
            f"  expected {float(mv[0]):+.0f}%→{float(mv[1]):+.0f}% · readiness {d.get('readiness')}"
        )
    drivers = d.get("main_drivers") or []
    if drivers:
        lines.append("  drivers: " + ", ".join(html.escape(str(x)) for x in drivers[:3]))
    return "\n".join(lines)


def format_scan(lists: dict[str, list[ExpansionOpportunity]], *, limit: int = 15) -> str:
    blocks: list[str] = []
    titles = {"pre_pump": "🟢 TOP PRE-PUMP", "pre_dump": "🔴 TOP PRE-DUMP"}
    for key in ("pre_pump", "pre_dump"):
        opps = lists.get(key) or []
        lines = [f"<b>{titles[key]}</b> (by OpportunityScore)"]
        if not opps:
            lines.append("  —")
        for i, o in enumerate(opps[:limit], 1):
            sym = html.escape(o.symbol.replace("USDT", "-USDT"))
            lines.append(
                f"  {i}. <code>{sym}</code> opp {o.meta.opportunity_score:.2f} · "
                f"trig {o.trigger_probability:.2f} · q {o.meta.expansion_quality:.2f} · {o.stage}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_universe_alert(alerts: dict[str, list[ExpansionOpportunity]]) -> str:
    """Batched TG digest for new universe expansion opportunities."""
    pump = alerts.get("pre_pump") or []
    dump = alerts.get("pre_dump") or []
    lines = ["🧨 <b>Expansion Universe Alert</b>", "<i>watch scan · not pinned anchors</i>", ""]
    titles = {"pre_pump": "🟢 PRE-PUMP", "pre_dump": "🔴 PRE-DUMP"}
    for key in ("pre_pump", "pre_dump"):
        opps = pump if key == "pre_pump" else dump
        lines.append(f"<b>{titles[key]}</b>")
        if not opps:
            lines.append("  —")
            continue
        for o in opps:
            sym = html.escape(o.symbol.replace("USDT", "-USDT"))
            rot = o.meta.sector_rotation
            rot_txt = f" · rot {rot:.2f}" if rot is not None else ""
            lines.append(
                f"  • <code>{sym}</code> opp <b>{o.meta.opportunity_score:.2f}</b> · "
                f"trig {o.trigger_probability:.2f} · q {o.meta.expansion_quality:.2f} · "
                f"{html.escape(o.stage)}{rot_txt}"
            )
        lines.append("")
    lines.append("<i>Expansion Engine · energy + fuel + trigger · not financial advice</i>")
    return "\n".join(lines)


def format_outcome_stats(
    summary: dict[str, Any],
    *,
    pending_reviews: int = 0,
    records: int | None = None,
) -> str:
    """Telegram block for the outcome learning ledger."""
    n_sig = int(records if records is not None else summary.get("signals") or 0)
    graded = int(summary.get("graded") or 0)
    hit = summary.get("real_hit_rate")
    avg_fav = summary.get("avg_favorable")
    lines = [
        "<b>📊 Expansion Outcomes</b>",
        f"Signals recorded: <b>{n_sig}</b>",
        f"Graded reviews: <b>{graded}</b>",
    ]
    if pending_reviews:
        lines.append(f"Pending reviews: <b>{pending_reviews}</b>")
    if hit is not None:
        lines.append(f"Real hit rate: <b>{float(hit) * 100:.1f}%</b>")
    if avg_fav is not None:
        lines.append(f"Avg favorable move: <b>{float(avg_fav):+.2f}%</b>")
    if graded == 0:
        lines.append("")
        lines.append("<i>Нет graded reviews — жди 24h+ после /expand на directional setup.</i>")
    return "\n".join(lines)


def format_calibration_report(report: dict[str, Any]) -> str:
    """Telegram block for block-weight calibration rollup."""
    status = str(report.get("status") or "unknown")
    samples = int(report.get("samples") or 0)
    lines = ["<b>⚖️ Expansion Calibration</b>", f"Status: <code>{html.escape(status)}</code>"]
    if status == "insufficient_samples":
        lines.append(f"Samples: {samples} (need ≥20 graded wins+losses)")
        lines.append("<i>Калибровка применится автоматически после накопления данных.</i>")
        return "\n".join(lines)
    wins = int(report.get("wins") or 0)
    losses = int(report.get("losses") or 0)
    lines.append(f"Samples: {samples} (W {wins} / L {losses})")
    mults = report.get("multipliers") if isinstance(report.get("multipliers"), dict) else {}
    if mults:
        lines.append("")
        lines.append("Top weight shifts:")
        ranked = sorted(mults.items(), key=lambda kv: abs(float(kv[1]) - 1.0), reverse=True)
        for name, mult in ranked[:8]:
            arrow = "↑" if float(mult) > 1.0 else ("↓" if float(mult) < 1.0 else "·")
            lines.append(f"  {arrow} <code>{html.escape(name)}</code> ×{float(mult):.3f}")
    applied = report.get("computed_at")
    if applied:
        lines.append("")
        lines.append(f"<i>Written {html.escape(str(applied)[:19])} · reload on next config load</i>")
    return "\n".join(lines)


def format_review_summary(summary: dict[str, Any]) -> str:
    """Telegram ack after a manual outcome review pass."""
    graded = int(summary.get("graded") or 0)
    missing = int(summary.get("missing_price") or 0)
    lines = ["<b>🔄 Expansion Review</b>"]
    if graded:
        lines.append(f"Graded: <b>{graded}</b> horizon(s)")
        if summary.get("calibration") == "refreshed":
            lines.append("Calibration: <b>refreshed</b>")
    else:
        lines.append("Nothing due — all horizons current or no pending signals.")
    if missing:
        lines.append(f"Missing price: {missing} symbol(s)")
    return "\n".join(lines)


def serialize_opportunity(opp: ExpansionOpportunity) -> dict[str, Any]:
    return opp.to_dict()


__all__ = [
    "format_calibration_report",
    "format_expansion_card",
    "format_expansion_section",
    "format_expansion_section_from_dict",
    "format_outcome_stats",
    "format_review_summary",
    "format_scan",
    "format_universe_alert",
    "serialize_opportunity",
]

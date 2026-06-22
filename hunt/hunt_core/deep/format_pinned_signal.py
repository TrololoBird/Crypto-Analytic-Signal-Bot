"""Pinned Telegram signal formatter — L5 product."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.deep.verdict_v2.types import ScenarioVerdict


def _px(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def format_pinned_signal(row: dict[str, Any], verdict: ScenarioVerdict | None = None) -> str:
    v2 = verdict or row.get("verdict_v2")
    if v2 is None:
        return ""
    sym = html.escape(str(row.get("symbol") or ""))
    dec = v2.signal_decision
    path = v2.expected_path
    cat = v2.catalyst
    strength = v2.signal_strength
    frag = v2.fragility
    tq = v2.trade_quality
    plan = dec.trade_plan or v2.trade_plan

    action = dec.action.upper()
    emoji = {"LONG": "🟢", "SHORT": "🔴", "WAIT": "⏸"}.get(action, "⏸")
    lines = [
        f"{emoji} <b>{sym} — {action}</b>",
        f"Path: <b>{html.escape(path.type.replace('_', ' '))}</b> · {html.escape(path.narrative[:80])}",
    ]
    if cat.trigger_level:
        lines.append(f"Catalyst: {html.escape(cat.label)} @ <code>{_px(cat.trigger_level)}</code>")
    else:
        lines.append(f"Catalyst: {html.escape(cat.label)}")

    if plan and action in {"LONG", "SHORT"}:
        lo, hi = plan.entry_zone
        lines.extend(
            [
                f"Entry: <code>{_px(lo)}</code> – <code>{_px(hi)}</code> ({plan.entry_type})",
                f"SL: <code>{_px(plan.stop_loss)}</code>",
                f"TP1: <code>{_px(plan.take_profit_1)}</code> ({plan.rr_tp1:.1f}R)",
                f"TP2: <code>{_px(plan.take_profit_2)}</code> ({plan.rr_tp2:.1f}R)",
                f"TP3: <code>{_px(plan.take_profit_3)}</code> ({plan.rr_tp3:.1f}R)",
            ]
        )
    elif plan:
        lines.append(f"Levels (advisory): SL <code>{_px(plan.stop_loss)}</code> · TP1 <code>{_px(plan.take_profit_1)}</code>")

    move = path.expected_move_pct
    time_h = path.expected_time_h
    lines.append(f"Move: {move[0]:.1f}–{move[1]:.1f}% · {time_h[0]:.0f}–{time_h[1]:.0f}h")
    lines.append(
        f"Strength <b>{strength.label}</b> ({strength.score:.2f}) · "
        f"Fragility <b>{frag.label}</b> · Trade <b>{tq.verdict}</b>"
    )
    lines.append(f"<i>{html.escape(strength.disclaimer)}</i>")
    if dec.gates_failed:
        lines.append(f"<i>WAIT gates: {html.escape(', '.join(dec.gates_failed))}</i>")

    summary = row.get("verdict_v2_summary") if isinstance(row.get("verdict_v2_summary"), dict) else {}
    act = str(summary.get("activation") or "")
    if not act:
        from hunt_core.deep.verdict_v2.activation import assess_activation

        act = str(assess_activation(row, summary).get("state") or "")
    if act and act != "idle":
        lines.append(f"Activation: <b>{html.escape(act.replace('_', ' '))}</b>")

    alt_paths = summary.get("secondary_paths") or []
    if alt_paths:
        lines.append(f"<i>Alt paths: {html.escape(', '.join(str(p) for p in alt_paths[:2]))}</i>")

    from hunt_core.deep.verdict_v2.config import load_verdict_v2_config

    if load_verdict_v2_config().tg_verbose:
        pc = v2.pattern_confidence
        pat_line = f"Patterns: {pc.primary.id}"
        if pc.alternatives:
            pat_line += " · " + ", ".join(a.id for a in pc.alternatives)
        lines.append(f"<i>{html.escape(pat_line)} (spread {pc.spread:.2f})</i>")
        lines.append(f"<i>Driver: {html.escape(v2.market_driver.primary)} · topo {html.escape(v2.horizon_topology.kind)}</i>")

    return "\n".join(lines)

"""Pinned Telegram signal formatter — L5 product."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.deep.verdict_v2.types import ScenarioVerdict

_ACTION_RU = {"LONG": "ЛОНГ", "SHORT": "ШОРТ", "WAIT": "ЖДЁМ"}
_STRENGTH_RU = {"strong": "сильный", "moderate": "средний", "weak": "слабый"}
_FRAG_RU = {"low": "низкая", "medium": "средняя", "high": "высокая"}
_TRADE_RU = {"good": "хорошая", "marginal": "слабая", "not trade": "не торговать", "not_trade": "не торговать"}
_GATE_RU = {
    "timing_c": "ждём подтверждения",
    "timing_a": "рано — ждём",
    "timing_b": "нет триггера",
    "conviction": "низкая убеждённость",
    "structure": "структура не подтверждена",
    "confluence": "нет слияния факторов",
    "rr": "R:R недостаточный",
    "regime": "рыночный режим против",
}
_ENTRY_TYPE_RU = {"market": "рынок", "pullback_limit": "лимит на откате", "limit": "лимит"}


def _px(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def _dedup_narrative(path_type: str, narrative: str) -> str:
    """Remove path-type prefix from narrative if it's a near-duplicate."""
    base = path_type.replace("_", " ").lower()
    narr = narrative.strip()
    if narr.lower().startswith(base):
        remainder = narr[len(base):].lstrip(" ·,—")
        if remainder:
            return remainder
        return narr
    return narr


def _gate_label(code: str) -> str:
    return _GATE_RU.get(code.lower(), code.replace("_", " "))


def format_pinned_signal(row: dict[str, Any], verdict: ScenarioVerdict | None = None) -> str:
    v2 = verdict or row.get("verdict_v2")
    if v2 is None:
        return ""
    sym = html.escape(str(row.get("symbol") or "").replace("USDT", "-USDT"))
    dec = v2.signal_decision
    path = v2.expected_path
    cat = v2.catalyst
    strength = v2.signal_strength
    frag = v2.fragility
    tq = v2.trade_quality
    plan = dec.trade_plan or v2.trade_plan

    action = dec.action.upper()
    action_ru = _ACTION_RU.get(action, action)
    emoji = {"LONG": "🟢", "SHORT": "🔴", "WAIT": "⏳"}.get(action, "⏳")

    path_type_str = html.escape(path.type.replace("_", " "))
    narr = _dedup_narrative(path.type, path.narrative[:80])
    narr_str = html.escape(narr) if narr else ""

    lines = [f"{emoji} <b>{sym} — {action_ru}</b>"]
    if narr_str:
        lines.append(f"Сценарий: <b>{path_type_str}</b> · {narr_str}")
    else:
        lines.append(f"Сценарий: <b>{path_type_str}</b>")

    if cat.trigger_level:
        lines.append(f"Катализатор: {html.escape(cat.label)} @ <code>{_px(cat.trigger_level)}</code>")
    else:
        lines.append(f"Катализатор: {html.escape(cat.label)}")

    if plan and action in {"LONG", "SHORT"}:
        lo, hi = plan.entry_zone
        entry_type_ru = _ENTRY_TYPE_RU.get(plan.entry_type, plan.entry_type)
        lines.extend(
            [
                f"Зона входа: <code>{_px(lo)}</code> – <code>{_px(hi)}</code> ({entry_type_ru})",
                f"Stop-loss: <code>{_px(plan.stop_loss)}</code>",
                f"TP1: <code>{_px(plan.take_profit_1)}</code> ({plan.rr_tp1:.1f}R)",
                f"TP2: <code>{_px(plan.take_profit_2)}</code> ({plan.rr_tp2:.1f}R)",
                f"TP3: <code>{_px(plan.take_profit_3)}</code> ({plan.rr_tp3:.1f}R)",
            ]
        )
    elif plan and tq.verdict not in {"marginal", "not trade", "not_trade"}:
        # Only show advisory levels for WAIT if trade quality is acceptable
        lines.append(
            f"Уровни (справочно): SL <code>{_px(plan.stop_loss)}</code> · "
            f"TP1 <code>{_px(plan.take_profit_1)}</code>"
        )

    move = path.expected_move_pct
    time_h = path.expected_time_h
    lines.append(f"Движение: {move[0]:.1f}–{move[1]:.1f}% · {time_h[0]:.0f}–{time_h[1]:.0f}h")

    strength_ru = _STRENGTH_RU.get(strength.label, strength.label)
    frag_ru = _FRAG_RU.get(frag.label, frag.label)
    trade_ru = _TRADE_RU.get(tq.verdict, tq.verdict)
    lines.append(
        f"Сила <b>{strength_ru}</b> ({strength.score:.2f}) · "
        f"Хрупкость <b>{frag_ru}</b> · Сделка <b>{trade_ru}</b>"
    )
    lines.append("<i>ранг, не вероятность</i>")

    if dec.gates_failed:
        gate_labels = ", ".join(_gate_label(g) for g in dec.gates_failed)
        lines.append(f"<i>Ожидаем: {html.escape(gate_labels)}</i>")

    summary = row.get("verdict_v2_summary") if isinstance(row.get("verdict_v2_summary"), dict) else {}
    act = str(summary.get("activation") or "")
    if not act:
        from hunt_core.deep.verdict_v2.activation import assess_activation

        act = str(assess_activation(row, summary).get("state") or "")
    _ACT_RU = {
        "in_entry_zone": "в зоне входа",
        "above_zone": "выше зоны",
        "below_zone": "ниже зоны",
        "approaching": "подходит к зоне",
        "breakout": "пробой",
        "reversal": "разворот",
    }
    if act and act != "idle":
        act_ru = _ACT_RU.get(act, act.replace("_", " "))
        lines.append(f"Активация: <b>{html.escape(act_ru)}</b>")

    # Alt paths: only show if different from main path type
    alt_paths = summary.get("secondary_paths") or []
    main_path_key = path.type.lower().replace("_", " ")
    unique_alts = [
        p for p in alt_paths[:2]
        if str(p).lower().replace("_", " ") != main_path_key
    ]
    if unique_alts:
        lines.append(f"<i>Альт. сценарий: {html.escape(', '.join(str(p) for p in unique_alts))}</i>")

    from hunt_core.deep.verdict_v2.config import load_verdict_v2_config

    if load_verdict_v2_config().tg_verbose:
        pc = v2.pattern_confidence
        pat_line = f"Паттерны: {pc.primary.id}"
        if pc.alternatives:
            pat_line += " · " + ", ".join(a.id for a in pc.alternatives)
        lines.append(f"<i>{html.escape(pat_line)} (разброс {pc.spread:.2f})</i>")
        lines.append(
            f"<i>Драйвер: {html.escape(v2.market_driver.primary)} · "
            f"топо {html.escape(v2.horizon_topology.kind)}</i>"
        )

    return "\n".join(lines)

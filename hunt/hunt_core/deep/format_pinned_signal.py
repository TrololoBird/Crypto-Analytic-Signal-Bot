"""Pinned Telegram signal formatter — L5 product."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.deep.verdict_v2.types import ScenarioVerdict, TradePlan

_ACTION_RU = {"LONG": "ЛОНГ", "SHORT": "ШОРТ", "WAIT": "ЖДЁМ"}
_STRENGTH_RU = {"strong": "сильный", "moderate": "средний", "weak": "слабый"}
_FRAG_RU = {"low": "низкая", "medium": "средняя", "high": "высокая"}
_TRADE_RU = {"favorable": "благоприятная", "marginal": "слабая", "poor": "плохая"}
_GATE_RU = {
    "timing_c": "нет подтверждения тайминга",
    "timing_a": "рано — ждём",
    "timing_b": "нет триггера",
    "conviction": "низкая убеждённость",
    "structure": "структура не подтверждена",
    "confluence": "нет слияния факторов",
    "rr": "R:R ниже порога",
    "rr_primary": "R:R ниже порога",
    "strength": "сила сигнала низкая",
    "fragility": "высокая хрупкость",
    "regime": "рыночный режим против",
    "context_conflict": "контекст против сделки",
    "catalyst": "катализатор слабый",
    "no_plan": "нет торгового плана",
    "coverage": "недостаточно данных",
    "path_neutral": "нейтральный путь",
}
_ENTRY_TYPE_RU = {"market": "рынок", "pullback_limit": "лимит на откате", "limit": "лимит"}

# Path-type and pattern vocabulary → Russian (display only; raw keys drive logic).
_PATH_RU = {
    "continuation_down": "продолжение вниз",
    "continuation_up": "продолжение вверх",
    "breakout_down": "пробой вниз",
    "breakout_up": "пробой вверх",
    "pullback_down": "откат вниз",
    "pullback_up": "откат вверх",
    "squeeze_down": "сжатие вниз",
    "squeeze_up": "сжатие вверх",
    "bull_pullback": "бычий откат",
    "bear_rally": "медвежий отскок",
    "long_squeeze": "сквиз лонгов",
    "short_squeeze": "сквиз шортов",
    "liquidity_sweep": "снятие ликвидности",
    "stop_hunt": "охота за стопами",
    "distribution": "распределение",
    "bear_continuation": "медвежье продолжение",
    "accumulation": "накопление",
    "range": "боковик",
}
# Softer display when strength is weak / no trade permission (flow not proven).
_PATH_SOFT_RU = {
    "pullback_down": "локальный откат",
    "pullback_up": "локальный отскок",
    "distribution": "локальное сопротивление",
    "accumulation": "локальный спрос",
    "liquidity_sweep": "отказ у уровня",
    "stop_hunt": "отскок от уровня",
    "continuation_down": "локальное давление вниз",
    "continuation_up": "локальное давление вверх",
}
_REJECT_HYPOTHESIS_GATES = frozenset(
    {"strength", "rr", "rr_primary", "context_conflict", "fragility"}
)
_FLOW_HEAVY_PATTERNS = frozenset({"distribution", "bear_continuation", "accumulation"})
_CATALYST_RU = {
    "Sweep highs then reject": "снятие хаёв и отказ",
    "Sweep lows then reclaim": "снятие лоёв и возврат",
    "Lose POC": "потеря POC",
    "Reclaim POC": "возврат над POC",
    "Key level break": "пробой ключевого уровня",
    "Funding flush": "сброс фандинга",
    "Flow confirmation": "подтверждение потока",
}


def _ru_path(token: str, *, soft: bool = False) -> str:
    key = token.strip().lower().replace(" ", "_")
    if soft:
        return _PATH_SOFT_RU.get(key, _PATH_RU.get(key, token.strip()))
    return _PATH_RU.get(key, token.strip())


def _ru_narrative(narrative: str, *, soft: bool = False) -> str:
    """Translate engine narrative '<path> via <pattern>' to Russian."""
    narrative = narrative.strip()
    if narrative.startswith("via "):
        return f"через {_ru_path(narrative[4:], soft=soft)}"
    if " via " in narrative:
        left, right = narrative.split(" via ", 1)
        return f"{_ru_path(left, soft=soft)} через {_ru_path(right, soft=soft)}"
    return _ru_path(narrative, soft=soft)


def _ru_catalyst(label: str) -> str:
    return _CATALYST_RU.get(label.strip(), label.strip())


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


def _use_soft_narrative(
    action: str,
    strength_label: str,
    strength_score: float,
    *,
    pattern_id: str = "",
    flow_evidence: bool = False,
) -> bool:
    """Weaken flow-heavy pattern words when there is no trade permission or flow proof."""
    if action == "WAIT":
        return True
    if strength_label == "weak" or strength_score < 0.52:
        return True
    if pattern_id in _FLOW_HEAVY_PATTERNS and not flow_evidence:
        return True
    return False


def _flow_evidence_present(v2: ScenarioVerdict) -> bool:
    flow = v2.engine_outputs.get("flow") if isinstance(v2.engine_outputs, dict) else None
    if flow is None:
        return False
    return bool(flow.evidence) and max(float(flow.long), float(flow.short)) >= 0.55


def _hypothesis_header(action: str, gates_failed: tuple[str, ...] | list[str]) -> str:
    if action != "WAIT":
        return "Сценарий"
    codes = {str(g).lower() for g in gates_failed}
    if codes & _REJECT_HYPOTHESIS_GATES:
        return "Гипотеза (отклонена)"
    return "Гипотеза (не для входа)"


def _show_activation_block(action: str, trade_verdict: str) -> bool:
    """Geographic activation must not imply entry when decision is WAIT or trade is poor."""
    if action != "WAIT":
        return True
    return trade_verdict == "favorable"


def _gate_diagnostic_lines(
    gates_failed: tuple[str, ...] | list[str],
    reconcile_caveats: tuple[str, ...] | list[str],
    *,
    strength_score: float,
    strength_min: float,
    fragility_score: float,
    fragility_max: float,
    plan: TradePlan | None,
    rr_min: float,
) -> list[str]:
    """Explicit failed-gate diagnostics for operator audit."""
    lines: list[str] = []
    seen: set[str] = set()
    for raw in gates_failed:
        code = str(raw).lower()
        if code == "strength":
            line = f"• strength {strength_score:.2f} < {strength_min:.2f}"
        elif code in {"rr", "rr_primary"} and plan is not None:
            line = f"• R:R {plan.rr_primary:.2f} < {rr_min:.2f}"
        elif code == "fragility":
            line = f"• fragility {fragility_score:.2f} > {fragility_max:.2f}"
        elif code == "timing_c":
            line = "• timing C не готов (ждём closed-bar)"
        elif code == "context_conflict":
            line = "• контекст против сделки"
        elif code == "coverage":
            line = f"• {_gate_label(code)}"
        else:
            line = f"• {_gate_label(code)}"
        if line not in seen:
            lines.append(line)
            seen.add(line)
    for caveat in reconcile_caveats[:3]:
        text = str(caveat).strip()
        if not text:
            continue
        dom_hint = "DOM" in text or "стакан" in text
        line = f"• {text}" if not dom_hint else f"• DOM: {text}"
        if line not in seen:
            lines.append(line)
            seen.add(line)
    return lines


def _why_wait_lines(
    gates_failed: tuple[str, ...] | list[str],
    reconcile_caveats: tuple[str, ...] | list[str],
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for code in gates_failed:
        label = _gate_label(str(code))
        if label not in seen:
            lines.append(f"• {label}")
            seen.add(label)
    for caveat in reconcile_caveats[:3]:
        text = str(caveat).strip()
        if text and text not in seen:
            lines.append(f"• {text}")
            seen.add(text)
    return lines


def format_pinned_signal(row: dict[str, Any], verdict: ScenarioVerdict | None = None) -> str:
    v2 = verdict or row.get("verdict_v2")
    # Defensive: a JSONL-roundtripped row carries verdict_v2 as a plain dict, which
    # has no ``.signal_decision``. Callers should use the summary fallback; bail here
    # rather than raising AttributeError mid-render.
    if not isinstance(v2, ScenarioVerdict):
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
    from hunt_core.deep.verdict_v2.config import load_verdict_v2_config

    cfg = load_verdict_v2_config()
    pattern_id = v2.pattern_confidence.primary.id
    flow_ok = _flow_evidence_present(v2)
    soft_narr = _use_soft_narrative(
        action,
        strength.label,
        strength.score,
        pattern_id=pattern_id,
        flow_evidence=flow_ok,
    )

    path_type_str = html.escape(_ru_path(path.type, soft=soft_narr))
    narr = _dedup_narrative(path.type, path.narrative[:80])
    narr_str = html.escape(_ru_narrative(narr, soft=soft_narr)) if narr else ""

    lines = [f"{emoji} <b>{sym} — {action_ru}</b>"]
    if action == "WAIT":
        lines.append("📌 <b>Итог: НЕТ СДЕЛКИ</b> · вход не разрешён")
        reasons = _gate_diagnostic_lines(
            dec.gates_failed,
            v2.reconcile_caveats,
            strength_score=strength.score,
            strength_min=cfg.gates.strength_min,
            fragility_score=frag.score,
            fragility_max=cfg.gates.fragility_max,
            plan=plan,
            rr_min=cfg.gates.rr_primary_min,
        )
        if reasons:
            hdr = (
                "Причины отказа"
                if {str(g).lower() for g in dec.gates_failed} & _REJECT_HYPOTHESIS_GATES
                else "Почему ждём"
            )
            lines.append(f"{hdr}:")
            lines.extend(html.escape(line) for line in reasons)

    scenario_hdr = _hypothesis_header(action, dec.gates_failed)
    if narr_str and narr_str != path_type_str:
        lines.append(f"{scenario_hdr}: <b>{path_type_str}</b> · {narr_str}")
    else:
        lines.append(f"{scenario_hdr}: <b>{path_type_str}</b>")

    cat_label_ru = html.escape(_ru_catalyst(cat.label))
    if action == "WAIT":
        lines.append(f"Условие гипотезы: {cat_label_ru}" + (
            f" @ <code>{_px(cat.trigger_level)}</code>" if cat.trigger_level else ""
        ))
    elif cat.trigger_level:
        lines.append(f"Катализатор: {cat_label_ru} @ <code>{_px(cat.trigger_level)}</code>")
    else:
        lines.append(f"Катализатор: {cat_label_ru}")

    if plan and action in {"LONG", "SHORT"}:
        lo, hi = plan.entry_zone
        entry_type_ru = _ENTRY_TYPE_RU.get(plan.entry_type, plan.entry_type)
        rr_label = plan.rr_base_label if plan.plan_lifecycle != "active" else "R:R (от входа)"
        lines.extend(
            [
                f"Зона входа: <code>{_px(lo)}</code> – <code>{_px(hi)}</code> ({entry_type_ru})",
                f"Stop-loss: <code>{_px(plan.stop_loss)}</code>",
                f"TP1: <code>{_px(plan.take_profit_1)}</code> ({plan.rr_tp1:.1f}R · {rr_label})",
                f"TP2: <code>{_px(plan.take_profit_2)}</code> ({plan.rr_tp2:.1f}R)",
                f"TP3: <code>{_px(plan.take_profit_3)}</code> ({plan.rr_tp3:.1f}R)",
            ]
        )
    elif plan and tq.verdict == "favorable":
        # Only show advisory levels for WAIT when trade quality is favorable;
        # marginal/poor WAITs must not surface actionable SL/TP (R:R often < 1).
        lines.append(
            f"Уровни (справочно): SL <code>{_px(plan.stop_loss)}</code> · "
            f"TP1 <code>{_px(plan.take_profit_1)}</code>"
        )

    move = path.expected_move_pct
    time_h = path.expected_time_h
    lines.append(f"Движение: {move[0]:.1f}–{move[1]:.1f}% · {time_h[0]:.0f}–{time_h[1]:.0f}ч")

    strength_ru = _STRENGTH_RU.get(strength.label, strength.label)
    frag_ru = _FRAG_RU.get(frag.label, frag.label)
    trade_ru = _TRADE_RU.get(tq.verdict, tq.verdict)
    strength_note = " · <i>индекс, не P(win)</i>" if action == "WAIT" else ""
    lines.append(
        f"Сила сигнала <b>{strength_ru}</b> ({strength.score:.2f}){strength_note} · "
        f"Хрупкость <b>{frag_ru}</b> · Сделка <b>{trade_ru}</b>"
    )
    if v2.reconcile_caveats and action != "WAIT":
        lines.append(f"⚠️ <i>{html.escape(v2.reconcile_caveats[0])}</i>")

    summary = row.get("verdict_v2_summary") if isinstance(row.get("verdict_v2_summary"), dict) else {}
    act = str(summary.get("activation") or "")
    if not act:
        from hunt_core.deep.verdict_v2.activation import assess_activation

        act = str(assess_activation(row, summary).get("state") or "")
    _ACT_RU = {
        "in_entry_zone": "в зоне входа",
        "at_catalyst": "на уровне катализатора",
        "near_catalyst": "близко к катализатору",
        "near_entry": "подходит к зоне",
        "above_zone": "выше зоны",
        "below_zone": "ниже зоны",
        "approaching": "подходит к зоне",
        "breakout": "пробой",
        "reversal": "разворот",
    }
    if act and act != "idle" and _show_activation_block(action, tq.verdict):
        act_ru = _ACT_RU.get(act, act.replace("_", " "))
        lines.append(f"Активация: <b>{html.escape(act_ru)}</b>")

    # Plan activation is a directional-trade artifact — never on WAIT/poor rows.
    evt = summary.get("activation_event")
    if action in {"LONG", "SHORT"} and isinstance(evt, dict) and evt.get("event") == "plan_activated":
        try:
            fill = float(evt.get("fill_reference") or 0)
        except (TypeError, ValueError):
            fill = 0.0
        if fill > 0:
            rr_base = str(evt.get("rr_base_label") or "R:R (от входа)")
            lines.append(
                f"✅ <b>План активирован</b> @ <code>{_px(fill)}</code> · "
                f"TP1 {float(evt.get('rr_tp1') or 0):.1f}R · "
                f"TP2 {float(evt.get('rr_tp2') or 0):.1f}R · "
                f"TP3 {float(evt.get('rr_tp3') or 0):.1f}R · <i>{html.escape(rr_base)}</i>"
            )

    # Alt paths: only show if different from main path type
    alt_paths = summary.get("secondary_paths") or []
    main_path_key = path.type.lower().replace("_", " ")
    unique_alts = [
        p for p in alt_paths[:2]
        if str(p).lower().replace("_", " ") != main_path_key
    ]
    if unique_alts:
        alt_ru = ", ".join(_ru_path(str(p), soft=soft_narr) for p in unique_alts)
        lines.append(f"<i>Альт. сценарий: {html.escape(alt_ru)}</i>")

    if cfg.tg_verbose:
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


__all__ = [
    "_flow_evidence_present",
    "_gate_diagnostic_lines",
    "_hypothesis_header",
    "_show_activation_block",
    "_use_soft_narrative",
    "_why_wait_lines",
    "format_pinned_signal",
]

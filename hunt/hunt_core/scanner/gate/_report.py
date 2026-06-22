"""Report stack — collect_report_blockers and evaluate_* entry points."""
from __future__ import annotations

from typing import Any

from hunt_core.contract import compute_setup_risk_reward
from hunt_core.scanner.gate._filters import hard_filter_blocks as _hard_filter_blocks
from hunt_core.scanner.gate._freshness import classify_delivery_tier
from hunt_core.scanner.gate._lifecycle import lifecycle_dict as _lifecycle_dict
from hunt_core.scanner.gate._lifecycle_gates import collect_lifecycle_blockers
from hunt_core.scanner.gate._registry import pipeline_pre_blockers as _pipeline_pre_blockers
from hunt_core.scanner.gate._ev import (
    filter_ev_primary_legacy_blockers,
    setup_conviction_pct,
    setup_p_win,
)
from hunt_core.scanner.gate._rr import (
    effective_min_rr as _effective_min_rr,
    late_dump_depth_chase_block as _late_dump_depth_chase_block,
    short_dump_delivery_too_late as _short_dump_delivery_too_late,
    tp2_room_blocks as _tp2_room_blocks,
)
from hunt_core.scanner.gate._types import GateResult, REPORT_BLOCK_PRIORITY
from hunt_core.scanner.gate.policy import direction_block_reason, run_declarative_delivery_gates
from hunt_core.params.store import delivery_thresholds, effective_hunt_params


def log_strategic_shadow(
    *,
    symbol: str,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    code: str,
    message: str,
) -> None:
    from hunt_core.scanner.detect.calibrate import symbol_state_tier
    from hunt_core.shared.ledger.shadow import append_shadow_reject, shadow_record_from_delivery

    n = int((row.get("feature_window_n") or row.get("lake_bars") or 0))
    append_shadow_reject(
        {
            **shadow_record_from_delivery(
                symbol=symbol,
                direction=direction,
                row=row,
                setup=setup,
                blockers=[code],
                no_signal_reason=message,
                symbol_state_tier=symbol_state_tier(n),
            ),
            "shadow_mode": True,
        }
    )


def log_delivery_shadow(
    *,
    symbol: str,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    blockers: list[GateResult],
) -> None:
    from hunt_core.scanner.detect.calibrate import symbol_state_tier
    from hunt_core.shared.ledger.shadow import append_shadow_reject, shadow_record_from_delivery

    if not blockers:
        return
    n = int((row.get("feature_window_n") or row.get("lake_bars") or 0))
    append_shadow_reject(
        shadow_record_from_delivery(
            symbol=symbol,
            direction=direction,
            row=row,
            setup=setup,
            blockers=[b.code for b in blockers],
            no_signal_reason=blockers[0].message,
            symbol_state_tier=symbol_state_tier(n),
        )
    )



def collect_report_blockers(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
    sniper_config: Any | None = None,
    fast_lane: bool = False,
) -> list[GateResult]:
    """All current blockers for /signals and live delivery, sorted by operator priority."""
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    lc = _lifecycle_dict(lifecycle)
    r = row or {}
    dl = delivery_thresholds(sym)
    p_win = setup_p_win(setup)
    conviction = setup_conviction_pct(setup, direction=direction)
    min_forming_p = float(dl.get("min_p_win_forming", 0.35))
    confirmed = bool(setup.get("confirmed") or setup.get("intrabar_confirmed"))
    blockers: list[GateResult] = []

    dir_block = direction_block_reason(direction)
    if dir_block:
        blockers.append(GateResult(False, dir_block, f"Direction policy: {dir_block}"))

    blockers.extend(
        _pipeline_pre_blockers(
            direction=direction,
            setup=setup,
            row=r,
            lifecycle=lc,
            symbol=sym,
            sniper_config=sniper_config,
            fast_lane=fast_lane,
        )
    )

    if setup.get("watch_only") and not setup.get("intrabar_confirmed"):
        blockers.append(
            GateResult(
                False,
                "watch_only",
                "Monitor-only (continuation) — не для delivery",
            )
        )

    phase = str(lc.get("phase") or "")
    blockers.extend(
        collect_lifecycle_blockers(
            setup,
            direction=direction,
            lifecycle=lc,
            row=r,
            symbol=sym,
        )
    )

    from hunt_core.scanner.gate._phase_matrix import phase_matrix_gate

    pm_blocked, pm_reason = phase_matrix_gate(phase, direction)
    if pm_blocked:
        blockers.append(GateResult(False, "phase_matrix_disable", pm_reason))

    delivery_tier = classify_delivery_tier(
        direction=direction,
        setup=setup,
        row=r,
        lifecycle=lc,
    )
    if delivery_tier is not None:
        decl = run_declarative_delivery_gates(
            r,
            setup,
            direction,
            lc,
            tier=delivery_tier,
            symbol=sym,
        )
        if decl is not None:
            blockers.append(decl)

    decl_evaluated = delivery_tier is not None

    if direction == "long":
        fall = float(lc.get("fall_from_high_pct") or 0)
        if phase == "dump_active":
            blockers.append(
                GateResult(
                    False,
                    "long_blocked_mid_dump",
                    "Лонг в mid-dump запрещён — жди post_dump_bounce",
                )
            )
        elif phase not in {
            "post_dump_bounce",
            "impulse_initiating",
            "breakout_arming",
        }:
            hunt_high = float(
                r.get("impulse_high") or ((r.get("impulse") or {}).get("hunt_high")) or 0
            )
            price = float(r.get("price") or 0)
            if hunt_high > 0 and price > 0 and price < hunt_high * 0.90 and fall >= 12.0:
                blockers.append(
                    GateResult(
                        False,
                        "long_below_hunt_high",
                        f"Цена {price:.4g} < 90% hunt_high при fall {fall:.0f}%",
                    )
                )
        res = float(setup.get("resistance_break_level") or 0)
        px = float(r.get("price") or 0)
        r5_close = float((r.get("timeframes") or {}).get("5m_closed", {}).get("close") or 0)
        from hunt_core.scanner.gate._delivery_helpers import long_resistance_chase_veto

        if long_resistance_chase_veto(res, px, r5_close):
            blockers.append(
                GateResult(
                    False,
                    "long_below_resistance",
                    f"Цена {px:.4g} ниже resistance_break {res:.4g} — поздний chase",
                )
            )

    if p_win is not None and p_win < min_forming_p:
        blockers.append(
            GateResult(
                False,
                "below_forming_min",
                f"P(win) {p_win:.2f} < forming_min {min_forming_p:.2f}",
            )
        )
    elif p_win is None and conviction < cal.forming_min_score:
        blockers.append(
            GateResult(
                False,
                "below_forming_min",
                f"Conviction {conviction:.0f} < forming_min {cal.forming_min_score:.0f}",
            )
        )

    if not confirmed:
        blockers.append(
            GateResult(False, "not_confirmed", "Нет closed-bar confirm (5m/1m)")
        )

    filter_blocks = _hard_filter_blocks(
        setup.get("filter_blocks") or [],
        direction=direction,
        phase=phase,
        fall_from_high_pct=float(lc.get("fall_from_high_pct") or 0),
    )
    if filter_blocks:
        txt = ", ".join(str(b) for b in filter_blocks)
        blockers.append(GateResult(False, "filter_block", f"Фильтр тренда/VWAP: {txt}"))

    px = float(r.get("price") or 0)
    min_rr = _effective_min_rr(
        setup, direction=direction, symbol=sym, lc=lc, cal=cal
    )
    if _tp2_room_blocks(
        setup, price=px, min_room_pct=cal.tp2_min_room_pct, min_rr=min_rr
    ):
        blockers.append(
            GateResult(
                False,
                "tp2_too_close",
                f"TP2 слишком близко ({cal.tp2_min_room_pct:.0f}% room min)",
            )
        )

    if setup.get("levels_viable") is False:
        veto_list = setup.get("levels_veto") or []
        blockers.append(
            GateResult(
                False,
                "levels_veto",
                f"Уровни не viable: {', '.join(str(v) for v in veto_list) or 'структура'}",
            )
        )

    rr = compute_setup_risk_reward(setup, direction=direction)
    if rr is not None:
        setup["risk_reward"] = rr
    if not decl_evaluated:
        if rr is None:
            blockers.append(
                GateResult(False, "rr_missing", "R:R не вычислен — нет entry/SL/TP1")
            )
        elif float(rr) < min_rr:
            blockers.append(
                GateResult(False, "rr_below_min", f"R:R {float(rr):.2f} < min {min_rr:.2f}")
            )

    if direction == "short" and confirmed:
        late = _short_dump_delivery_too_late(lc, setup, symbol=sym)
        if late is not None:
            blockers.append(late)
        session = r.get("session") or {}
        pos = float(session.get("pos_in_range") or setup.get("context_pos_in_range") or 0.5)
        depth = _late_dump_depth_chase_block(
            fall=float(lc.get("fall_from_high_pct") or 0),
            pos_in_range=pos,
            phase=phase,
            hard=list(setup.get("confirm_hard") or []),
        )
        if depth is not None:
            blockers.append(depth)
        tf = r.get("timeframes") or {}
        has_bear_div = bool(
            (tf.get("1h") or {}).get("bearish_rsi_div")
            or (tf.get("4h") or {}).get("bearish_rsi_div")
        )
        _ = (phase, lc, session, has_bear_div, sym)  # fusion phase owns PRE/MID now

    seen: set[str] = set()
    unique: list[GateResult] = []
    for item in blockers:
        if item.code in seen:
            continue
        seen.add(item.code)
        unique.append(item)
    unique.sort(key=lambda g: REPORT_BLOCK_PRIORITY.get(g.code, 50))
    return filter_ev_primary_legacy_blockers(
        unique, setup, direction=direction, symbol=sym, row=row
    )


def primary_block_for_report(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> GateResult:
    """Report parity: same gate stack as live Telegram confirm (evaluate_delivery)."""
    from hunt_core.deliver.dispatch import evaluate_delivery

    base_row = dict(row) if isinstance(row, dict) else {}
    gate, tier = evaluate_delivery(
        base_row,
        direction=direction,
        setup=setup,
        lifecycle=lifecycle,
        symbol=symbol,
        refresh_live_price=False,
    )
    if not gate.ok:
        return gate
    if tier is None:
        return GateResult(False, "delivery_no_tier", "No ARMED/TRIGGERED tier")
    return GateResult(True, "ok", f"tier={tier}")


def evaluate_stale_advice(
    *,
    symbol: str,
    direction: str,
    lifecycle: Any | None,
    setup: dict[str, Any],
    sig: dict[str, Any],
) -> str | None:
    """Action hint for open tracker positions in /signals."""
    lc = _lifecycle_dict(lifecycle)
    phase = str(lc.get("phase") or "")
    bias = str(lc.get("recommended_bias") or "")

    if phase == "no_setup":
        return "💡 Auto-invalidate через 3 тика — lifecycle no_setup"
    if direction == "short" and phase == "post_dump_bounce":
        if sig.get("tp1_hit"):
            return "💡 Auto-invalidate — bounce + TP1 (тезис шорта исчерпан)"
        return "💡 Auto-invalidate через 3 тика — post_dump_bounce против шорта"
    if direction == "short" and lc.get("invalidate_short"):
        return "💡 Рекомендация: invalidate — lifecycle invalidate_short"
    if direction == "short" and bias == "long":
        return "💡 Тезис устарел (bias long) — держи по latch, новый шорт не уйдёт"
    if direction == "long" and bias == "short":
        return "💡 Тезис устарел (bias short) — держи по latch, новый лонг не уйдёт"
    if direction == "long" and phase in {"distribution", "exhaustion_at_high"}:
        return "💡 Лонг против distribution — рассмотри фиксацию / invalidate"
    if sig.get("tp1_hit") and not sig.get("tp2_hit"):
        pct = sig.get("partial_fixed_pct") or 80
        if sig.get("sl_at_breakeven"):
            return (
                f"💡 TP1 взят — зафиксируй {pct}% · SL на entry (безубыток) · "
                f"остаток на TP2"
            )
        return f"💡 TP1 взят — зафиксируй {pct}% · остаток на TP2 / SL"
    cal = effective_hunt_params(symbol)
    dl = delivery_thresholds(symbol.upper())
    p_win = setup_p_win(setup)
    conviction = setup_conviction_pct(setup, direction=direction)
    min_confirm_p = float(dl.get("min_p_win", 0.42))
    if not bool(setup.get("confirmed")) and (
        (p_win is not None and p_win >= min_confirm_p)
        or (p_win is None and conviction >= cal.confirm_min_score)
    ):
        return "💡 P(win) достаточен — жди closed-bar confirm для re-alert"
    return None


def format_setup_snapshot(
    setup: dict[str, Any],
    *,
    direction: str,
    latch_score: Any,
    lifecycle: Any | None = None,
) -> str:
    """Compact live setup line — avoids duplicating the primary block reason."""
    lc = _lifecycle_dict(lifecycle)
    conviction = setup_conviction_pct(setup, direction=direction)
    p_win = setup_p_win(setup)
    phase = str(setup.get("phase") or "—")
    latch = latch_score if latch_score not in (None, "", "—") else "—"
    confirm = "да" if bool(setup.get("confirmed")) else "нет"
    bias = str(lc.get("recommended_bias") or "—")
    strength = f"P {p_win:.0%}" if p_win is not None else f"conv {conviction:.0f}"
    return (
        f"Сетап: confirm={confirm} · {strength} (открыт {latch}) · "
        f"{phase} · bias={bias}"
    )


def evaluate_alert_gate(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
    sniper_config: Any | None = None,
    fast_lane: bool = False,
) -> GateResult:
    """Single gate stack (#26): same blockers as /signals report, first by priority."""
    blockers = collect_report_blockers(
        setup,
        direction=direction,
        symbol=symbol,
        lifecycle=lifecycle,
        row=row,
        sniper_config=sniper_config,
        fast_lane=fast_lane,
    )
    if blockers:
        log_delivery_shadow(
            symbol=symbol,
            direction=direction,
            setup=setup,
            row=row or {},
            blockers=blockers,
        )
        return blockers[0]
    if row is not None:
        from hunt_core.scanner.gate._maps import map_opposing_bias_veto

        veto, reason = map_opposing_bias_veto(row, direction=direction)
        if veto and reason:
            br = GateResult(False, reason, f"Map liquidity veto: {reason}")
            log_delivery_shadow(
                symbol=symbol,
                direction=direction,
                setup=setup,
                row=row,
                blockers=[br],
            )
            return br
    if not bool(setup.get("confirmed")) and not setup.get("intrabar_confirmed"):
        return GateResult(False, "not_confirmed", "Нет closed-bar confirm (5m/1m)")
    return GateResult(True, "ok", "Все гейты пройдены — алерт разрешён")


def evaluate_formation(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
) -> GateResult:
    """Pre-confirm setup state for /signals and logs."""
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    lc = _lifecycle_dict(lifecycle)
    dl = delivery_thresholds(sym)
    p_win = setup_p_win(setup)
    conviction = setup_conviction_pct(setup, direction=direction)
    min_forming_p = float(dl.get("min_p_win_forming", 0.35))
    min_confirm_p = float(dl.get("min_p_win", 0.42))
    phase = str(setup.get("phase") or "—")
    confirmed = bool(setup.get("confirmed"))

    if confirmed:
        label = f"P {p_win:.0%}" if p_win is not None else f"conv {conviction:.0f}"
        return GateResult(True, "confirmed", f"Confirm есть · phase={phase} · {label}")

    if p_win is not None and p_win < min_forming_p:
        return GateResult(
            False,
            "forming_low",
            f"Формирование слабое: P(win) {p_win:.2f} < {min_forming_p:.2f}",
        )
    if p_win is None and conviction < cal.forming_min_score:
        return GateResult(
            False,
            "forming_low",
            f"Формирование слабое: conviction {conviction:.0f} < {cal.forming_min_score:.0f}",
        )

    gaps: list[str] = []
    if not (setup.get("confirm_hard") or []):
        gaps.append("нет structural hard")
    if p_win is not None and p_win < min_confirm_p:
        gaps.append(f"P(win) {p_win:.2f} < confirm {min_confirm_p:.2f}")
    elif p_win is None and conviction < cal.confirm_min_score:
        gaps.append(f"conviction {conviction:.0f} < confirm {cal.confirm_min_score:.0f}")
    gap_txt = ", ".join(gaps) if gaps else "ждём closed-bar"
    bias = str(lc.get("recommended_bias") or "—")
    label = f"P {p_win:.0%}" if p_win is not None else f"conv={conviction:.0f}"
    return GateResult(
        False,
        "forming",
        f"Формируется {phase} · {label} · bias={bias} · {gap_txt}",
    )


__all__ = [
    "collect_report_blockers",
    "evaluate_alert_gate",
    "evaluate_formation",
    "evaluate_stale_advice",
    "format_setup_snapshot",
    "primary_block_for_report",
]

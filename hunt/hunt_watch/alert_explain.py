"""Human-readable alert gates — single source for _should_alert and /signals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hunt_watch.lifecycle import blocks_premature_exhaustion_short
from hunt_watch.param_store import delivery_thresholds, effective_hunt_params
from hunt_watch.phase_matrix_gate import phase_matrix_gate
from hunt_watch.prep_shadow_tracker import prep_shadow_delivery_fuel_adjustment
from hunt_watch.signal_engine import long_resistance_chase_veto

BOUNCE_MIN_RISK_REWARD = 0.5
PINNED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT"})


@dataclass(frozen=True, slots=True)
class GateResult:
    ok: bool
    code: str
    message: str


# Display order for /signals (most actionable first — independent of alert short-circuit).
REPORT_BLOCK_PRIORITY: dict[str, int] = {
    "stale_no_setup": 0,
    "invalidate_short": 1,
    "bias_conflict": 2,
    "short_entry_not_ok": 3,
    "long_blocked_mid_dump": 4,
    "long_below_resistance": 5,
    "long_below_hunt_high": 5,
    "lifecycle_veto_hard": 6,
    "below_forming_min": 7,
    "phase_matrix_disable": 8,
    "premature_exhaustion": 9,
    "not_confirmed": 10,
    "filter_block": 11,
    "not_anomaly": 12,
    "levels_veto": 13,
    "rr_below_min": 14,
    "tp2_too_close": 15,
    "delivery_fuel_low": 16,
    "delivery_confluence_low": 17,
    "exhaustion_fade_weak": 18,
    "accumulation_long_weak": 19,
    "exhaustion_strong_trend": 18,
    "impulse_session_weak": 19,
    "impulse_oi_weak": 19,
    "prep_shadow_tighten": 16,
}


def _lifecycle_dict(lifecycle: Any | None) -> dict[str, Any]:
    if isinstance(lifecycle, dict):
        return lifecycle
    if lifecycle is None:
        return {}
    return {
        "phase": lifecycle.phase.value,
        "recommended_bias": lifecycle.recommended_bias,
        "fall_from_high_pct": lifecycle.fall_from_high_pct,
        "bounce_from_low_pct": lifecycle.bounce_from_low_pct,
        "short_entry_ok": lifecycle.short_entry_ok,
        "short_confirm_ok": lifecycle.short_confirm_ok,
        "invalidate_short": lifecycle.invalidate_short,
    }


def _min_rr(symbol: str, direction: str, lc: dict[str, Any]) -> float:
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    if sym in PINNED_SYMBOLS:
        return cal.pinned_min_risk_reward
    phase = str(lc.get("phase") or "")
    if direction == "long" and phase == "post_dump_bounce":
        return BOUNCE_MIN_RISK_REWARD
    return cal.min_risk_reward


def _setup_fuel(setup: dict[str, Any], direction: str) -> float:
    key = "dump_fuel" if direction == "short" else "long_fuel"
    alt = "dump_score" if direction == "short" else "long_score"
    return float(setup.get(key) or setup.get(alt) or 0)


_PUMP_PHASES_LONG = frozenset({"impulse_initiating", "breakout_arming"})
_FADE_PHASES_SHORT = frozenset({"exhaustion_at_high", "distribution"})
_DUMP_CONTINUATION_PHASES = frozenset(
    {"dump_active", "distribution", "impulse_initiating"}
)
_STRUCTURAL_DUMP_MARKERS = (
    "close_below_support",
    "below_support",
    "live_below_support",
    "lost_support",
    "bear_cascade",
)


def _structural_dump_hard(hard: list[Any]) -> bool:
    return any(
        any(marker in str(h) for marker in _STRUCTURAL_DUMP_MARKERS) for h in hard
    )


def _dump_continuation_short_ok(
    setup: dict[str, Any],
    *,
    phase: str,
    lc: dict[str, Any],
    fuel: float,
    cal_min_fuel: float,
) -> bool:
    """Structural dump leg after pump collapse — lifecycle short_entry_ok may be false."""
    del fuel, cal_min_fuel  # confirm path already enforced fuel; re-check caused false blocks
    if not bool(setup.get("confirmed")):
        return False
    fall = float(lc.get("fall_from_high_pct") or 0)
    if phase not in _DUMP_CONTINUATION_PHASES or fall < 12.0:
        return False
    return _structural_dump_hard(setup.get("confirm_hard") or [])


_DUMP_CONTINUATION_MIN_RR = 1.10


def _effective_min_rr(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lc: dict[str, Any],
    fuel: float,
    cal: HuntCalibratedParams,
) -> float:
    base = _min_rr(symbol, direction, lc)
    if direction != "short":
        return base
    if _dump_continuation_short_ok(
        setup,
        phase=str(lc.get("phase") or ""),
        lc=lc,
        fuel=fuel,
        cal_min_fuel=cal.confirm_min_score,
    ):
        return min(base, _DUMP_CONTINUATION_MIN_RR)
    return base


def _tp2_room_blocks(
    setup: dict[str, Any],
    *,
    price: float,
    min_room_pct: float,
    min_rr: float,
) -> bool:
    """Block only when TP2 cramped *and* R:R not already satisfied on TP1 path."""
    tp2 = float(setup.get("tp2") or 0)
    if price <= 0 or tp2 <= 0:
        return False
    room = abs(price - tp2) / price * 100.0
    if room >= min_room_pct:
        return False
    rr = setup.get("risk_reward")
    if rr is not None and float(rr) >= min_rr:
        return False
    if _structural_dump_hard(setup.get("confirm_hard") or []) and len(
        setup.get("confirm_hard") or []
    ) >= 2:
        return False
    return True


def _hard_filter_blocks(
    blocks: list[Any],
    *,
    direction: str,
    phase: str,
    fall_from_high_pct: float = 0.0,
) -> list[str]:
    """Phase-aware filter severity. VWAP/ADX trend filters describe *trend
    continuation* — on an initial pump leg (long) or exhaustion fade (short)
    that is the setup itself, not a contra-signal:
    - VELVET +96% leg: vwap_overbought_5.3atr blocked every confirmed long;
    - BEAT 8.37 top: adx1h_uptrend_* blocked 253 confirmed exhaustion shorts.
    Outside those phases the filters stay hard."""
    out: list[str] = []
    for raw in blocks:
        tag = str(raw)
        if direction == "long" and phase in _PUMP_PHASES_LONG and (
            tag.startswith("vwap_overbought") or tag.startswith("adx1h_uptrend")
        ):
            continue
        if direction == "short" and tag.startswith("adx1h_uptrend"):
            if phase in _FADE_PHASES_SHORT or phase in _DUMP_CONTINUATION_PHASES:
                continue
        if direction == "short" and tag.startswith("vwap_oversold"):
            if phase in {"dump_active", "distribution"}:
                continue
            if phase == "impulse_initiating" and fall_from_high_pct >= 8.0:
                continue
        out.append(tag)
    return out


def _structural_hard_count(hard: list[Any], *, direction: str) -> int:
    keys_short = (
        "close_below_support",
        "rejection",
        "cascade",
        "ws_liq",
        "5m_close",
        "15m_close",
        "bear_cascade",
    )
    keys_long = (
        "close_above_resistance",
        "bounce",
        "cascade",
        "broke_resistance",
        "5m_close",
        "bull_cascade",
        "ws_taker_buy",
    )
    keys = keys_short if direction == "short" else keys_long
    return sum(1 for h in hard if any(k in str(h) for k in keys))


def _delivery_quality_gate(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: dict[str, Any],
    fuel: float,
    row: dict[str, Any],
) -> GateResult | None:
    """High-conviction Telegram delivery — target 70% WR (confluence + fuel floor)."""
    dl = delivery_thresholds(symbol)
    base_min_fuel = float(dl.get("min_fuel", 72.0))
    hard = [str(h) for h in (setup.get("confirm_hard") or [])]
    fuel_adj, adj_reason = prep_shadow_delivery_fuel_adjustment()
    waive_prep_bump = (
        direction == "short"
        and bool(setup.get("confirmed"))
        and fuel >= base_min_fuel
        and _structural_dump_hard(hard)
    )
    if waive_prep_bump:
        fuel_adj = 0.0
        adj_reason = None
    min_fuel = max(68.0, base_min_fuel + fuel_adj)
    min_struct = int(dl.get("min_structural_hard", 2))
    struct_n = _structural_hard_count(hard, direction=direction)
    phase = str(lifecycle.get("phase") or "")

    if fuel < min_fuel:
        tier_note = f" (adj +{fuel_adj:.0f})" if fuel_adj > 0 else ""
        shadow_note = f" · {adj_reason}" if adj_reason and fuel_adj != 0 else ""
        return GateResult(
            False,
            "delivery_fuel_low" if fuel_adj <= 0 else "prep_shadow_tighten",
            f"Delivery fuel {fuel:.0f} < {min_fuel:.0f}{tier_note} (70% WR tier){shadow_note}",
        )
    min_struct_eff = min_struct
    fall = float(lifecycle.get("fall_from_high_pct") or 0)
    if (
        direction == "short"
        and phase in _DUMP_CONTINUATION_PHASES
        and fall >= 12.0
        and fuel >= min_fuel
        and _structural_dump_hard(hard)
    ):
        min_struct_eff = 1
    if struct_n < min_struct_eff:
        return GateResult(
            False,
            "delivery_confluence_low",
            f"Structural hard {struct_n} < {min_struct_eff} (confluence gate)",
        )

    if direction == "short" and phase in _FADE_PHASES_SHORT:
        exh_min = float(dl.get("exhaustion_short_min_fuel", 78.0))
        tf = row.get("timeframes") or {}
        has_div = bool(
            (tf.get("1h") or {}).get("bearish_rsi_div")
            or (tf.get("4h") or {}).get("bearish_rsi_div")
        )
        closed_break = any("close_below_support" in h for h in hard)
        adx_max = float(dl.get("exhaustion_adx_max", 32.0))
        adx14 = float((tf.get("1h") or {}).get("adx14") or 0)
        if adx14 > adx_max and not has_div and not closed_break:
            return GateResult(
                False,
                "exhaustion_strong_trend",
                f"Fade при ADX1h {adx14:.0f} > {adx_max:.0f} — сильный тренд, жди div/break",
            )
        if fuel < exh_min and not has_div and not closed_break:
            return GateResult(
                False,
                "exhaustion_fade_weak",
                f"Fade-at-top fuel {fuel:.0f} < {exh_min:.0f} без div/closed break",
            )

    if direction == "long" and phase in _PUMP_PHASES_LONG:
        sess = row.get("session") or {}
        pos = float(sess.get("pos_in_range") or 0.5)
        min_pos = float(dl.get("impulse_long_min_pos", 0.52))
        hi = float(sess.get("high_24h") or 0)
        lo = float(sess.get("low_24h") or 0)
        px = float(row.get("price") or 0)
        need_mid = bool(dl.get("impulse_long_above_mid", True))
        mid = (hi + lo) / 2.0 if hi > lo else 0.0
        if pos < min_pos:
            return GateResult(
                False,
                "impulse_session_weak",
                f"Лонг-импульс: pos_in_range {pos:.2f} < {min_pos:.2f} — нет session momentum",
            )
        if need_mid and mid > 0 and px > 0 and px < mid:
            return GateResult(
                False,
                "impulse_session_weak",
                f"Цена {px:.4g} ниже mid 24h {mid:.4g} — слабый импульс сессии",
            )
        market = row.get("market") or {}
        oi_chg = market.get("oi_chg_1h")
        min_oi = float(dl.get("impulse_long_min_oi_chg_1h", 0.005))
        if oi_chg is not None:
            try:
                oi_f = float(oi_chg)
            except (TypeError, ValueError):
                oi_f = 0.0
            if oi_f < min_oi:
                return GateResult(
                    False,
                    "impulse_oi_weak",
                    f"OI 1h Δ {oi_f * 100:.2f}% < {min_oi * 100:.1f}% — нет притока позиций",
                )

    if direction == "long" and phase == "accumulation":
        acc_min = float(dl.get("accumulation_long_min_fuel", 74.0))
        chg24 = float(setup.get("context_chg_24h_pct") or row.get("chg_24h_pct") or 0)
        if fuel < acc_min and chg24 < -8.0:
            return GateResult(
                False,
                "accumulation_long_weak",
                f"Weak accumulation long fuel {fuel:.0f} < {acc_min:.0f} при chg24 {chg24:.1f}%",
            )

    return None


def _lifecycle_veto_hard(setup: dict[str, Any]) -> GateResult | None:
    for raw in setup.get("confirm_hard") or []:
        tag = str(raw)
        if tag.startswith("veto_lifecycle") or tag.startswith("veto_mtf"):
            label = "mtf_veto_hard" if tag.startswith("veto_mtf") else "lifecycle_veto_hard"
            return GateResult(False, label, f"Confirm veto: {tag}")
    return None


def _bias_conflict(direction: str, lc: dict[str, Any]) -> GateResult | None:
    bias = str(lc.get("recommended_bias") or "")
    if direction == "short" and bias == "long":
        return GateResult(False, "bias_conflict", "Bias long — открытый шорт против lifecycle")
    if direction == "long" and bias == "short":
        return GateResult(False, "bias_conflict", "Bias short — открытый лонг против lifecycle")
    return None


def collect_report_blockers(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> list[GateResult]:
    """All current blockers for /signals, sorted by operator priority."""
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    lc = _lifecycle_dict(lifecycle)
    r = row or {}
    sess = r.get("session") or {}
    fuel = _setup_fuel(setup, direction)
    confirmed = bool(setup.get("confirmed"))
    blockers: list[GateResult] = []

    phase = str(lc.get("phase") or "")
    if phase == "no_setup":
        blockers.append(
            GateResult(False, "stale_no_setup", "Lifecycle no_setup — сетап исчез")
        )

    pm_blocked, pm_reason = phase_matrix_gate(phase, direction)
    if pm_blocked:
        blockers.append(GateResult(False, "phase_matrix_disable", pm_reason))

    if direction == "short" and phase == "post_dump_bounce":
        blockers.append(
            GateResult(
                False,
                "short_blocked_bounce",
                "Шорт в post_dump_bounce запрещён — отскок после дампа",
            )
        )

    if direction == "short" and lc.get("invalidate_short"):
        blockers.append(
            GateResult(
                False,
                "invalidate_short",
                "Lifecycle: отскок/пробой вверх — шорт инвалидирован",
            )
        )

    bias_hit = _bias_conflict(direction, lc)
    if bias_hit is not None:
        blockers.append(bias_hit)

    veto = _lifecycle_veto_hard(setup)
    if veto is not None:
        blockers.append(veto)

    if direction == "short" and not lc.get("short_entry_ok", False):
        if not _dump_continuation_short_ok(
            setup,
            phase=phase,
            lc=lc,
            fuel=fuel,
            cal_min_fuel=cal.confirm_min_score,
        ):
            bias = str(lc.get("recommended_bias") or "—")
            blockers.append(
                GateResult(
                    False,
                    "short_entry_not_ok",
                    f"Lifecycle {phase or '—'} bias={bias} — вход в шорт запрещён",
                )
            )

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
        if long_resistance_chase_veto(res, px, r5_close):
            blockers.append(
                GateResult(
                    False,
                    "long_below_resistance",
                    f"Цена {px:.4g} ниже resistance_break {res:.4g} — поздний chase",
                )
            )

    if fuel < cal.forming_min_score:
        blockers.append(
            GateResult(
                False,
                "below_forming_min",
                f"Fuel {fuel:.0f} < forming_min {cal.forming_min_score:.0f}",
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

    sess = r.get("session") or {}
    chg24 = abs(float(r.get("chg_24h_pct") or 0))
    rng24 = float(sess.get("range_pct_24h") or 0)
    if not (
        bool(r.get("young_listing"))
        or chg24 >= cal.anomaly_min_chg_24h_pct
        or rng24 >= cal.anomaly_min_range_24h_pct
    ):
        blockers.append(
            GateResult(
                False,
                "not_anomaly",
                f"Не meme-аномалия: chg24={chg24:.1f}% range={rng24:.1f}% "
                f"(нужно ≥{cal.anomaly_min_chg_24h_pct}% или ≥{cal.anomaly_min_range_24h_pct}%)",
            )
        )

    px = float(r.get("price") or 0)
    min_rr = _effective_min_rr(
        setup, direction=direction, symbol=sym, lc=lc, fuel=fuel, cal=cal
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

    rr = setup.get("risk_reward")
    if rr is not None and float(rr) < min_rr:
        blockers.append(
            GateResult(False, "rr_below_min", f"R:R {float(rr):.2f} < min {min_rr:.2f}")
        )

    if direction == "short" and confirmed:
        session = r.get("session") or {}
        tf = r.get("timeframes") or {}
        has_bear_div = bool(
            (tf.get("1h") or {}).get("bearish_rsi_div")
            or (tf.get("4h") or {}).get("bearish_rsi_div")
        )
        blocked, prem_reason = blocks_premature_exhaustion_short(
            phase=phase,
            fall_from_high_pct=float(lc.get("fall_from_high_pct") or 0),
            bounce_from_low_pct=float(lc.get("bounce_from_low_pct") or 0),
            pos_in_range=float(session.get("pos_in_range") or 0.5),
            has_bear_div=has_bear_div,
            symbol=sym,
        )
        if blocked:
            hard = setup.get("confirm_hard") or []
            if not any("close_below_support" in str(h) for h in hard):
                blockers.append(
                    GateResult(
                        False,
                        "premature_exhaustion",
                        f"Ранний fade-at-top: {prem_reason}",
                    )
                )

    seen: set[str] = set()
    unique: list[GateResult] = []
    for item in blockers:
        if item.code in seen:
            continue
        seen.add(item.code)
        unique.append(item)
    unique.sort(key=lambda g: REPORT_BLOCK_PRIORITY.get(g.code, 50))
    return unique


def primary_block_for_report(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> GateResult:
    blockers = collect_report_blockers(
        setup, direction=direction, symbol=symbol, lifecycle=lifecycle, row=row
    )
    if blockers:
        return blockers[0]
    return evaluate_alert_gate(
        setup, direction=direction, symbol=symbol, lifecycle=lifecycle, row=row
    )


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
    if not bool(setup.get("confirmed")) and _setup_fuel(setup, direction) >= effective_hunt_params(
        symbol
    ).confirm_min_score:
        return "💡 Fuel достаточен — жди closed-bar confirm для re-alert"
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
    fuel = _setup_fuel(setup, direction)
    phase = str(setup.get("phase") or "—")
    latch = latch_score if latch_score not in (None, "", "—") else "—"
    confirm = "да" if bool(setup.get("confirmed")) else "нет"
    bias = str(lc.get("recommended_bias") or "—")
    return (
        f"Сетап: confirm={confirm} · fuel {fuel:.0f} (открыт {latch}) · "
        f"{phase} · bias={bias}"
    )


def evaluate_alert_gate(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> GateResult:
    """Mirror watch._should_alert with explicit Russian explanation."""
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    lc = _lifecycle_dict(lifecycle)
    r = row or {}

    if not bool(setup.get("confirmed")):
        return GateResult(False, "not_confirmed", "Нет closed-bar confirm (5m/1m)")

    phase = str(lc.get("phase") or "")
    pm_blocked, pm_reason = phase_matrix_gate(phase, direction)
    if pm_blocked:
        return GateResult(False, "phase_matrix_disable", pm_reason)

    fuel = _setup_fuel(setup, direction)
    if fuel < cal.forming_min_score:
        return GateResult(
            False,
            "below_forming_min",
            f"Fuel {fuel:.0f} < forming_min {cal.forming_min_score:.0f}",
        )

    blocks = _hard_filter_blocks(
        setup.get("filter_blocks") or [],
        direction=direction,
        phase=str(lc.get("phase") or ""),
        fall_from_high_pct=float(lc.get("fall_from_high_pct") or 0),
    )
    if blocks:
        txt = ", ".join(str(b) for b in blocks)
        return GateResult(False, "filter_block", f"Фильтр тренда/VWAP: {txt}")

    sess = r.get("session") or {}
    chg24 = abs(float(r.get("chg_24h_pct") or 0))
    rng24 = float(sess.get("range_pct_24h") or 0)
    if not (
        bool(r.get("young_listing"))
        or chg24 >= cal.anomaly_min_chg_24h_pct
        or rng24 >= cal.anomaly_min_range_24h_pct
    ):
        return GateResult(
            False,
            "not_anomaly",
            f"Не meme-аномалия: chg24={chg24:.1f}% range={rng24:.1f}% "
            f"(нужно ≥{cal.anomaly_min_chg_24h_pct}% или ≥{cal.anomaly_min_range_24h_pct}%)",
        )

    px = float(r.get("price") or 0)
    min_rr = _effective_min_rr(
        setup, direction=direction, symbol=sym, lc=lc, fuel=fuel, cal=cal
    )
    if _tp2_room_blocks(
        setup, price=px, min_room_pct=cal.tp2_min_room_pct, min_rr=min_rr
    ):
        return GateResult(
            False,
            "tp2_too_close",
            f"TP2 слишком близко ({cal.tp2_min_room_pct:.0f}% room min)",
        )

    if setup.get("levels_viable") is False:
        veto = setup.get("levels_veto") or []
        return GateResult(
            False,
            "levels_veto",
            f"Уровни не viable: {', '.join(str(v) for v in veto) or 'структура'}",
        )

    rr = setup.get("risk_reward")
    if rr is not None and float(rr) < min_rr:
        return GateResult(False, "rr_below_min", f"R:R {float(rr):.2f} < min {min_rr:.2f}")

    if direction == "short":
        phase = str(lc.get("phase") or "—")
        if phase == "post_dump_bounce":
            return GateResult(
                False,
                "short_blocked_bounce",
                "Шорт в post_dump_bounce запрещён — отскок после дампа, не fade",
            )
        if lc.get("invalidate_short"):
            return GateResult(
                False,
                "invalidate_short",
                "Lifecycle: отскок/пробой вверх — шорт инвалидирован",
            )
        if not lc.get("short_entry_ok", False) and not _dump_continuation_short_ok(
            setup,
            phase=phase,
            lc=lc,
            fuel=fuel,
            cal_min_fuel=cal.confirm_min_score,
        ):
            bias = str(lc.get("recommended_bias") or "—")
            return GateResult(
                False,
                "short_entry_not_ok",
                f"Lifecycle {phase} bias={bias} — вход в шорт запрещён",
            )
        session = r.get("session") or {}
        tf = r.get("timeframes") or {}
        has_bear_div = bool(
            (tf.get("1h") or {}).get("bearish_rsi_div")
            or (tf.get("4h") or {}).get("bearish_rsi_div")
        )
        blocked, prem_reason = blocks_premature_exhaustion_short(
            phase=str(lc.get("phase") or ""),
            fall_from_high_pct=float(lc.get("fall_from_high_pct") or 0),
            bounce_from_low_pct=float(lc.get("bounce_from_low_pct") or 0),
            pos_in_range=float(session.get("pos_in_range") or 0.5),
            has_bear_div=has_bear_div,
            symbol=sym,
        )
        if blocked:
            hard = setup.get("confirm_hard") or []
            if not any("close_below_support" in str(h) for h in hard):
                return GateResult(
                    False,
                    "premature_exhaustion",
                    f"Ранний fade-at-top: {prem_reason}",
                )

    if direction == "long":
        phase = str(lc.get("phase") or "")
        fall = float(lc.get("fall_from_high_pct") or 0)
        if phase == "dump_active":
            return GateResult(
                False,
                "long_blocked_mid_dump",
                "Лонг в mid-dump запрещён — жди post_dump_bounce",
            )
        if phase not in {"post_dump_bounce", "impulse_initiating", "breakout_arming"}:
            hunt_high = float(
                r.get("impulse_high") or ((r.get("impulse") or {}).get("hunt_high")) or 0
            )
            price = float(r.get("price") or 0)
            if hunt_high > 0 and price > 0 and price < hunt_high * 0.90 and fall >= 12.0:
                return GateResult(
                    False,
                    "long_below_hunt_high",
                    f"Цена {price:.4g} < 90% hunt_high при fall {fall:.0f}%",
                )
        res = float(setup.get("resistance_break_level") or 0)
        r5_close = float((r.get("timeframes") or {}).get("5m_closed", {}).get("close") or 0)
        if long_resistance_chase_veto(res, px, r5_close):
            return GateResult(
                False,
                "long_below_resistance",
                f"Цена {px:.4g} ниже resistance_break {res:.4g}",
            )

    delivery_block = _delivery_quality_gate(
        setup,
        direction=direction,
        symbol=sym,
        lifecycle=lc,
        fuel=fuel,
        row=r,
    )
    if delivery_block is not None:
        return delivery_block

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
    fuel = _setup_fuel(setup, direction)
    phase = str(setup.get("phase") or "—")
    confirmed = bool(setup.get("confirmed"))

    if confirmed:
        return GateResult(True, "confirmed", f"Confirm есть · phase={phase} · fuel={fuel:.0f}")

    if fuel < cal.forming_min_score:
        return GateResult(
            False,
            "forming_low",
            f"Формирование слабое: fuel {fuel:.0f} < {cal.forming_min_score:.0f}",
        )

    gaps: list[str] = []
    if not (setup.get("confirm_hard") or []):
        gaps.append("нет structural hard")
    if fuel < cal.confirm_min_score:
        gaps.append(f"fuel {fuel:.0f} < confirm {cal.confirm_min_score:.0f}")
    gap_txt = ", ".join(gaps) if gaps else "ждём closed-bar"
    bias = str(lc.get("recommended_bias") or "—")
    return GateResult(
        False,
        "forming",
        f"Формируется {phase} · fuel={fuel:.0f} · bias={bias} · {gap_txt}",
    )


INVALIDATE_LABELS: dict[str, str] = {
    "bounce_invalidate": "Отмена: lifecycle отскок — шорт больше не валиден",
    "trend_exhaustion": "Отмена: long в фазе exhaustion/distribution",
    "bias_flip": "Отмена: bias lifecycle сменился против позиции",
    "reclaim_invalidation": "Отмена: reclaim уровня инвалидации",
    "support_lost": "Отмена: потеря support (long)",
    "stop_hit": "Закрыто по Stop Loss",
    "tp1": "Закрыто по TP1",
    "tp2": "Закрыто по TP2",
    "legacy_unknown": "Закрыто (причина не зафиксирована в tracker)",
    "time_stall": "Закрыто: нет MFE за 8h — тезис не сработал",
}


def invalidate_detail_human(detail: str, *, reason: str = "") -> str:
    if reason and reason in INVALIDATE_LABELS:
        base = INVALIDATE_LABELS[reason]
        if detail and detail not in base:
            return f"{base} · {detail}"
        return base
    if detail:
        return detail
    return INVALIDATE_LABELS.get(reason, "Сигнал отменён")

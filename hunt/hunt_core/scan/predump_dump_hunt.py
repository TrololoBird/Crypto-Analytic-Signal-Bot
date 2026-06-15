"""Dump-hunt tier state + telegram (wave 3C)."""
from __future__ import annotations

import html
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

from hunt_core.data.universe import watchlist_flags
from hunt_core.domain.market_regime import HuntCalibratedParams
from hunt_core.params.store import (
    confirm_thresholds,
    dump_fast_confirm_enabled,
    effective_hunt_params,
    entry_confirm_tf,
    liquidation_thresholds,
    listings_thresholds,
    orderflow_thresholds,
    scoring_thresholds,
)
from hunt_core.paths import ADAPTIVE_THRESHOLDS, DUMP_HUNT_ALERT_STATE, EWMA_THRESHOLDS, IGNITION_STATE
from hunt_core.errors import optional_finite_float, require_mark_price


def _htf_bias_override(*args, **kwargs):
    from hunt_core.regime.leg_fsm import htf_bias_override
    return htf_bias_override(*args, **kwargs)



_TRIGGER_REASONS = frozenset({
    "1m_macd_cross_down",
    "1m_macd_hist_neg",
    "1m_macd_exhaust",
    "below_support",
    "hunt_short_confirmed",
})

_SETUP_REASON_PREFIXES = (
    "1h_rsi=",
    "15m_rsi=",
    "top_ls=",
    "funding_crowded=",
    "phase=",
)


def _fval(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _market_val(market: dict[str, Any], micro: dict[str, Any], key: str) -> Any:
    val = market.get(key)
    if val is not None:
        return val
    return micro.get(key)


def _setup_hits(reasons: list[str]) -> int:
    return sum(1 for r in reasons if r.startswith(_SETUP_REASON_PREFIXES))


def _has_trigger(reasons: list[str]) -> bool:
    return any(
        r in _TRIGGER_REASONS
        or r.startswith("fall_trigger=")
        or r.startswith("below_support=")
        for r in reasons
    )


def score_dump_init(
    *,
    row: dict[str, Any],
    micro: dict[str, Any],
    tf: dict[str, Any],
    prev: dict[str, Any] | None,
) -> tuple[int, list[str], str]:
    """Score 0-100 dump initiation readiness + human verdict."""
    reasons: list[str] = []
    score = 0
    dump = row.get("dump") or {}
    lc = row.get("lifecycle") or {}
    market = row.get("market") or row.get("positioning") or {}
    price = float(row.get("price") or 0)

    phase = str(lc.get("phase") or "")
    fall = float(lc.get("fall_from_high_pct") or 0)
    hunt_high = float((row.get("impulse") or {}).get("hunt_high") or row.get("impulse_high") or 0)
    price_f = float(price or row.get("price") or 0)

    from hunt_core.regime.leg_fsm import pre_dump_zone  # noqa: PLC0415

    at_pre_dump = pre_dump_zone(
        price=price_f,
        hunt_high=hunt_high,
        fall_from_high_pct=fall,
    )
    if phase in ("exhaustion_at_high", "distribution"):
        score += 18
        reasons.append(f"phase={phase}")
    if at_pre_dump:
        score += 14
        reasons.append(f"pre_dump_fall={fall:.1f}%")
    elif fall >= 1.5:
        score += min(15, int(fall * 3))
        reasons.append(f"fall_from_high={fall:.1f}%")

    support = float(dump.get("support_break_level") or dump.get("invalidation_above") or 0)
    if support > 0 and price < support and fall >= 2.5:
        score += 20
        reasons.append(f"below_support={support:.5f}")

    # --- REST flow ---
    taker5 = _fval(_market_val(market, micro, "taker_5m"))
    if taker5 is not None and taker5 < 0.98:
        score += 12
        reasons.append(f"taker_5m_sell={taker5:.3f}")
    elif taker5 is not None and taker5 > 1.05:
        score -= 8
        reasons.append(f"taker_5m_buy={taker5:.3f}")

    oi1h = _fval(_market_val(market, micro, "oi_chg_1h"))
    if oi1h is not None and oi1h < -0.005:
        score += 10
        reasons.append(f"oi_flush_1h={oi1h:.3f}")

    oi_z = _fval(market.get("oi_z") or micro.get("oi_z"))
    if oi_z is not None and oi_z > 1.5:
        score += 5
        reasons.append(f"oi_z_elevated={oi_z}")

    fund = _fval(_market_val(market, micro, "funding_pct"))
    if fund is not None and fund >= 0.55:
        score += 10
        reasons.append(f"funding_crowded={fund:.3f}%")
    elif fund is not None and fund > 0.15:
        score += 8
        reasons.append(f"funding_crowded={fund:.3f}%")

    top_ls = _fval(_market_val(market, micro, "top_ls_1h"))
    if top_ls is not None and top_ls >= 1.85:
        score += 6
        reasons.append(f"top_ls={top_ls:.2f}")

    depth = _fval(market.get("depth_imbalance"))
    if depth is not None and depth <= -0.08:
        score += 8
        reasons.append(f"ask_heavy={depth:.2f}")

    # --- Polars MTF setup ---
    m1 = tf.get("1m") or {}
    m5 = tf.get("5m") or {}
    m15 = tf.get("15m") or {}
    m1h = tf.get("1h") or {}

    rsi1h = _fval(m1h.get("closed_rsi14") or m1h.get("rsi14"))
    if rsi1h is not None and rsi1h >= 88:
        score += 10
        reasons.append(f"1h_rsi={rsi1h:.0f}")

    rsi15 = _fval(m15.get("closed_rsi14") or m15.get("rsi14"))
    if rsi15 is not None and rsi15 >= 78:
        score += 8
        reasons.append(f"15m_rsi={rsi15:.0f}")

    rsi5 = _fval(m5.get("closed_rsi14") or m5.get("rsi14"))
    if rsi5 is not None and rsi5 > 70:
        score += 6
        reasons.append(f"5m_rsi={rsi5:.0f}")

    macd5 = _fval(m5.get("closed_macd_hist"))
    if macd5 is not None and macd5 < 0:
        score += 10
        reasons.append("5m_macd_hist_neg")

    # --- Phase 2: 1m MACD trigger (fast dump) ---
    macd1 = _fval(m1.get("closed_macd_hist") if m1.get("closed_macd_hist") is not None else m1.get("macd_hist"))
    if macd1 is not None:
        if macd1 < 0:
            score += 14
            reasons.append("1m_macd_hist_neg")
        elif macd1 <= 0.0002:
            score += 12
            reasons.append("1m_macd_exhaust")

    if prev and macd1 is not None:
        prev_m1 = (prev.get("timeframes") or {}).get("1m") or {}
        prev_macd = _fval(
            prev_m1.get("closed_macd_hist")
            if prev_m1.get("closed_macd_hist") is not None
            else prev_m1.get("macd_hist")
        )
        if prev_macd is not None and prev_macd > 0.0003 and macd1 <= 0:
            score += 16
            reasons.append("1m_macd_cross_down")

    if fall >= 4.0:
        score += 10
        reasons.append(f"fall_trigger={fall:.1f}%")

    if dump.get("confirmed"):
        score += 25
        reasons.append("hunt_short_confirmed")
    elif float(dump.get("dump_score") or 0) >= 68:
        score += 8
        reasons.append(f"dump_score={dump.get('dump_score')}")

    if prev:
        prev_fall = float((prev.get("lifecycle") or {}).get("fall_from_high_pct") or 0)
        if fall > prev_fall + 0.4:
            score += 8
            reasons.append(f"fall_accel +{fall - prev_fall:.1f}%")
        prev_price = float(prev.get("price") or 0)
        if prev_price > 0 and price < prev_price * 0.997:
            score += 6
            reasons.append("price_break_down")

    setup_hits = _setup_hits(reasons)
    trigger = _has_trigger(reasons) or bool(dump.get("confirmed"))

    score = max(0, min(100, score))
    if dump.get("confirmed"):
        verdict = "DUMP_LIKELY"
    elif score >= 85 and trigger and setup_hits >= 2:
        verdict = "DUMP_LIKELY"
    elif score >= 70 and (trigger or (setup_hits >= 3 and fall >= 2.0)):
        verdict = "DUMP_ARMED"
    elif score >= 50 and setup_hits >= 2:
        verdict = "DUMP_WATCH"
    elif setup_hits >= 1:
        verdict = "DUMP_WATCH"
    else:
        verdict = "PUMP_RISK"

    # Squeeze trap: crowded setup + aggressive taker buy, no fall yet → do not arm entry.
    if (
        taker5 is not None
        and taker5 > 1.12
        and fall < 2.0
        and not trigger
        and not dump.get("confirmed")
    ):
        verdict = "DUMP_WATCH"
    return score, reasons, verdict



DumpHuntTier = Literal["prep", "armed", "likely", "confirmed"]

TIER_RANK = {"prep": 0, "armed": 1, "likely": 2, "confirmed": 3}

# One symbol = max 1 alert per window unless tier escalates (armed→likely→confirmed).
SYMBOL_COOLDOWN_MIN = 45
NEAR_TP1_PCT = 4.0

TIER_BADGE = {
    "prep": "🟠",
    "armed": "🔴",
    "likely": "🚨",
    "confirmed": "🔴",
}

TIER_TITLE = {
    "prep": "DUMP PREP — готовь шорт",
    "armed": "DUMP ARMED — вход близко",
    "likely": "DUMP LIKELY — открывай сделку",
    "confirmed": "DUMP CONFIRMED — вход",
}

TIER_RU = {
    "prep": "Наблюдение",
    "armed": "Скоро",
    "likely": "Готовься",
    "confirmed": "Confirm",
}


def _load_state() -> dict[str, Any]:
    if not DUMP_HUNT_ALERT_STATE.exists():
        return {}
    try:
        payload = json.loads(DUMP_HUNT_ALERT_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    DUMP_HUNT_ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    DUMP_HUNT_ALERT_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _sym_key(symbol: str) -> str:
    return symbol.strip().upper()


def display_short_setup(
    setup: dict[str, Any],
    *,
    price: float,
    lifecycle: dict[str, Any] | None = None,
    impulse_low: float = 0.0,
    atr15: float = 0.0,
) -> dict[str, Any]:
    """Use hunt setup levels as-is — single TP1 from structural_short_levels."""
    _ = (price, lifecycle, impulse_low, atr15)
    return dict(setup)


def dump_hunt_skip_reason(
    *,
    symbol: str,
    tier: DumpHuntTier,
    price: float,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return block reason or None if alert may send."""
    now = now or datetime.now(UTC)
    sym = _sym_key(symbol)
    lc = lifecycle or {}
    fall = float(lc.get("fall_from_high_pct") or 0)
    phase = str(lc.get("phase") or "")

    leg_tp1 = float(setup.get("leg_tp1") or setup.get("tp1") or 0)
    if leg_tp1 > 0 and price > 0:
        if price <= leg_tp1:
            return "past_leg_tp1"
        dist_pct = (price - leg_tp1) / price * 100.0
        if dist_pct <= NEAR_TP1_PCT and tier in ("prep", "armed"):
            return "near_leg_tp1"

    disp = display_short_setup(setup, price=price, lifecycle=lc)
    disp_tp1 = float(disp.get("tp1") or 0)
    if disp_tp1 > 0 and price <= disp_tp1:
        return "past_display_tp1"

    if phase == "post_dump_bounce" and tier in ("prep", "armed"):
        return "post_dump_bounce"

    state = _load_state()
    sym_state = state.get(sym) if isinstance(state.get(sym), dict) else {}
    last_at_raw = sym_state.get("last_at")
    last_tier = str(sym_state.get("last_tier") or "")
    if last_at_raw:
        try:
            last_at = datetime.fromisoformat(str(last_at_raw))
        except ValueError:
            last_at = None
        if last_at is not None:
            elapsed = now - last_at
            new_rank = TIER_RANK[tier]
            old_rank = TIER_RANK.get(last_tier, -1)  # type: ignore[arg-type]
            if new_rank <= old_rank and elapsed < timedelta(minutes=SYMBOL_COOLDOWN_MIN):
                return "cooldown_same_tier"
            if new_rank < old_rank:
                return "tier_downgrade"
            if new_rank == old_rank and elapsed < timedelta(minutes=SYMBOL_COOLDOWN_MIN):
                return "cooldown_repeat"

    return None


def dump_hunt_cooldown_ok(
    symbol: str,
    tier: DumpHuntTier,
    *,
    now: datetime | None = None,
    price: float = 0.0,
    setup: dict[str, Any] | None = None,
    lifecycle: dict[str, Any] | None = None,
) -> bool:
    if setup is None:
        return True
    return dump_hunt_skip_reason(
        symbol=symbol,
        tier=tier,
        price=price,
        setup=setup,
        lifecycle=lifecycle,
        now=now,
    ) is None


def mark_dump_hunt_sent(
    symbol: str,
    tier: DumpHuntTier,
    *,
    now: datetime | None = None,
    price: float = 0.0,
) -> None:
    now = now or datetime.now(UTC)
    sym = _sym_key(symbol)
    state = _load_state()
    sym_state = dict(state.get(sym) or {})
    sym_state["last_at"] = now.isoformat()
    sym_state["last_tier"] = tier
    sym_state["last_price"] = price
    state[sym] = sym_state
    _save_state(state)


def _fmt_price(value: Any) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v >= 100:
        return f"{v:.2f}"
    if v >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def _pct_from_entry(entry: float, target: float) -> str:
    if entry <= 0 or target <= 0:
        return ""
    pct = (entry - target) / entry * 100.0
    return f"{pct:.1f}%"


def format_dump_hunt_telegram(
    *,
    symbol: str,
    tier: DumpHuntTier,
    price: float,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
    chg_24h: float | None = None,
    dump_init_score: int | None = None,
    dump_reasons: list[str] | None = None,
    note: str = "",
    impulse_low: float = 0.0,
    atr15: float = 0.0,
) -> str:
    sym = html.escape(symbol.replace("USDT", "-USDT"))
    lc = lifecycle or {}
    disp = display_short_setup(
        setup, price=price, lifecycle=lc, impulse_low=impulse_low, atr15=atr15
    )
    fuel = float(disp.get("dump_fuel") or disp.get("dump_score") or 0)
    score = disp.get("dump_score")
    phase = html.escape(str(disp.get("phase") or "—"))
    lc_phase = html.escape(str(lc.get("phase") or "—"))
    fall = lc.get("fall_from_high_pct")
    fall_txt = f" · fall {float(fall):.1f}%" if fall is not None else ""

    ez = disp.get("entry_zone") or [price, price]
    entry_lo = _fmt_price(ez[0] if len(ez) >= 1 else price)
    entry_hi = _fmt_price(ez[1] if len(ez) >= 2 else price)
    sl = _fmt_price(disp.get("stop_loss"))
    tp1 = disp.get("tp1")
    tp2 = disp.get("tp2")
    tp1_pct = _pct_from_entry(price, float(tp1)) if tp1 else ""
    tp2_pct = _pct_from_entry(price, float(tp2)) if tp2 else ""
    support = disp.get("support_break_level")

    badge = TIER_BADGE[tier]
    tier_ru = TIER_RU.get(tier, tier)
    chg_txt = f" · 24h <code>{chg_24h:.1f}%</code>" if chg_24h is not None else ""
    init_txt = (
        f" · deep score <code>{dump_init_score}</code>"
        if dump_init_score is not None
        else ""
    )

    lines = [
        f"{badge} <b>{tier_ru} · SHORT · {sym}</b>",
        f"Цена <code>{_fmt_price(price)}</code>{chg_txt}{init_txt}{fall_txt}",
        f"Lifecycle <code>{lc_phase}</code> · setup <code>{phase}</code> · fuel <code>{fuel:.0f}</code>"
        + (f" · score <code>{float(score):.0f}</code>" if score is not None else ""),
        f"📍 Вход <code>{entry_lo}–{entry_hi}</code> · SL <code>{sl}</code>",
    ]
    if support:
        lines.append(f"Support break <code>{_fmt_price(support)}</code>")
    if tp1:
        tp1_lbl = disp.get("tp1_label") or "TP1"
        tp1_line = f"🎯 {html.escape(str(tp1_lbl))} <code>{_fmt_price(tp1)}</code>"
        if tp1_pct:
            tp1_line += f" (<b>-{tp1_pct}</b>)"
        if tp2:
            tp2_lbl = disp.get("tp2_label") or "TP2"
            tp2_line = f" · {html.escape(str(tp2_lbl))} <code>{_fmt_price(tp2)}</code>"
            if tp2_pct:
                tp2_line += f" (<b>-{tp2_pct}</b>)"
            tp1_line += tp2_line
        lines.append(tp1_line)

    hard = disp.get("confirm_hard") or []
    if hard:
        lines.append(f"Signals: <code>{html.escape(', '.join(str(h) for h in hard[:6]))}</code>")
    if dump_reasons:
        lines.append(f"Deep: <code>{html.escape(', '.join(dump_reasons[:6]))}</code>")
    if note:
        lines.append(f"<i>{html.escape(note)}</i>")
    lines.append(f"<i>Этап: {tier_ru} · dump-hunt · не auto-trade</i>")
    return "\n".join(lines)


def tier_from_verdict(verdict: str, *, confirmed: bool) -> DumpHuntTier | None:
    if confirmed:
        return "confirmed"
    v = verdict.upper()
    if v == "DUMP_LIKELY":
        return "likely"
    if v == "DUMP_ARMED":
        return "armed"
    if v == "DUMP_WATCH":
        return "prep"
    return None


async def maybe_send_dump_hunt_telegram(
    broadcaster: Any,
    *,
    symbol: str,
    tier: DumpHuntTier,
    message: str,
    now: datetime | None = None,
    price: float = 0.0,
    setup: dict[str, Any] | None = None,
    lifecycle: dict[str, Any] | None = None,
) -> bool:
    if setup is not None:
        reason = dump_hunt_skip_reason(
            symbol=symbol,
            tier=tier,
            price=price,
            setup=setup,
            lifecycle=lifecycle,
            now=now,
        )
        if reason:
            return False
    result = await broadcaster.send_html(message)
    if result.status != "sent":
        return False
    mark_dump_hunt_sent(symbol, tier, now=now, price=price)
    return True



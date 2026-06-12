"""Dump-hunt Telegram — early entry alerts before full closed-bar confirm."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from hunt_watch.paths import DUMP_HUNT_ALERT_STATE

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
    title = TIER_TITLE[tier]
    chg_txt = f" · 24h <code>{chg_24h:.1f}%</code>" if chg_24h is not None else ""
    init_txt = (
        f" · deep score <code>{dump_init_score}</code>"
        if dump_init_score is not None
        else ""
    )

    lines = [
        f"{badge} <b>{title}</b> {sym}",
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
    lines.append("<i>Dump-hunt · ручной вход · не auto-trade</i>")
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

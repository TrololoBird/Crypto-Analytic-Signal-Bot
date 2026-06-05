"""WATCH paths: radar funnel + delivered-signal ACTION escalation (no auto-publish)."""

from __future__ import annotations

import html
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.delivery.ops_webhook import send_ops_webhook_alert
from bot.delivery.telegram_routing import operator_dm_enabled, send_operator_html
from bot.domain.delivery_policy import effective_action_min_score
from bot.domain.limit_entry import limit_delivery_ready, resolve_late_entry_chase_pct
from bot.market.radar_state import SymbolTier
from bot.runtime.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from bot.domain.schemas import PreparedSymbol, Signal
    from bot.runtime.bot import SignalBot

LOG = logging.getLogger("bot.runtime.watch_escalation")


def watch_ready_for_action_escalation(
    signal: Signal,
    prepared: PreparedSymbol | None,
    *,
    settings: Any,
) -> tuple[bool, str]:
    """True when a WATCH plan matured enough to consider manual ACTION promotion."""
    if not bool(getattr(settings.delivery, "watch_escalation_enabled", True)):
        return False, "disabled"
    score = float(signal.score or 0.0)
    action_min = effective_action_min_score(settings, signal.symbol)
    if score < action_min:
        return False, "score_below_action"

    mark_price = getattr(signal, "mark_price", None)
    if prepared is not None and mark_price is None:
        mark_price = getattr(prepared, "mark_price", None)
    chase_pct = resolve_late_entry_chase_pct(settings)
    ready, reason, _details = limit_delivery_ready(
        direction=str(signal.direction or ""),
        mark_price=float(mark_price) if mark_price is not None else None,
        entry_low=float(signal.entry_low),
        entry_high=float(signal.entry_high),
        stop=float(signal.stop),
        chase_pct=chase_pct,
    )
    if not ready:
        return False, reason or "limit_not_ready"
    return True, "zone_ready"


def _escalation_state_key(signal: Signal) -> str:
    tracking_id = getattr(signal, "tracking_id", None)
    if tracking_id:
        return str(tracking_id)
    return str(getattr(signal, "signal_key", "") or "")


def _watch_escalation_states(bot: SignalBot) -> dict[str, str]:
    states = getattr(bot, "_watch_escalation_states", None)
    if states is None:
        states = {}
        bot._watch_escalation_states = states
    return states


async def maybe_notify_watch_escalation(
    bot: SignalBot,
    signal: Signal,
    prepared: PreparedSymbol | None,
) -> None:
    ok, note = watch_ready_for_action_escalation(signal, prepared, settings=bot.settings)
    states = _watch_escalation_states(bot)
    key = _escalation_state_key(signal)
    previous = states.get(key, "")
    states[key] = note
    if not ok or note != "zone_ready" or previous == "zone_ready":
        return

    if not operator_dm_enabled(bot, "send_watch_escalation"):
        return

    sym = html.escape(signal.symbol)
    setup = html.escape(signal.setup_id)
    direction = html.escape(str(signal.direction or ""))
    ref = html.escape(str(getattr(signal, "tracking_ref", "") or ""))
    text = (
        "<b>👀 WATCH → ACTION?</b>\n"
        f"{sym} {direction} · {setup} · <code>#{ref}</code>\n"
        f"Score <code>{float(signal.score or 0) * 100:.0f}%</code> · {html.escape(note)}\n"
        "<i>Ручное решение - бот не эскалирует в канал автоматически</i>"
    )
    notified = False
    try:
        notified = bool(await send_operator_html(bot, text))
        if notified:
            LOG.info(
                "watch escalation hint sent | symbol=%s setup=%s note=%s",
                signal.symbol,
                signal.setup_id,
                note,
            )
    except DEFENSIVE_EXC:
        LOG.debug("watch escalation notify failed", exc_info=True)

    if await send_ops_webhook_alert(
        bot,
        event="watch_escalation",
        text=text,
        extra={
            "symbol": signal.symbol,
            "setup_id": signal.setup_id,
            "direction": signal.direction,
            "tracking_ref": getattr(signal, "tracking_ref", None),
            "note": note,
        },
    ):
        notified = True

    if notified:
        states[key] = "zone_ready"


# --- Radar funnel WATCH (not on deep shortlist; never bypasses delivery) ---

_MAX_RADAR_DM_PER_REFRESH = 3


def _shortlist_symbol_set(bot: SignalBot) -> set[str]:
    return {
        str(getattr(item, "symbol", "")).strip().upper()
        for item in (getattr(bot, "_shortlist", None) or [])
        if str(getattr(item, "symbol", "")).strip()
    }


def collect_radar_watch_rows(
    bot: SignalBot,
    store: object,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Warm/hot radar symbols with screener flags but not in production shortlist."""
    cfg = bot.settings.universe.radar
    if not cfg.enabled:
        return []
    ts = float(now if now is not None else time.monotonic())
    on_shortlist = _shortlist_symbol_set(bot)
    rows: list[dict[str, Any]] = []
    iter_states = getattr(store, "iter_states", None)
    if not callable(iter_states):
        return rows
    for state in iter_states():
        if not state.flags:
            continue
        if state.tier not in {SymbolTier.WARM, SymbolTier.HOT}:
            continue
        sym = str(state.symbol).strip().upper()
        if sym in on_shortlist:
            continue
        rows.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "symbol": sym,
                "tier": state.tier.value,
                "flags": list(state.flags),
                "reasons": list(state.promotion_reasons),
                "prescore_boost": float(state.prescore_boost or 0.0),
                "quote_volume": float(state.quote_volume or 0.0),
                "change_24h_pct": float(state.price_change_pct_24h or 0.0),
                "age_since_update_s": round(max(0.0, ts - float(state.last_update_ts or 0.0)), 2),
            }
        )
    rows.sort(key=lambda row: (row["tier"] != SymbolTier.HOT.value, -row["prescore_boost"]))
    return rows


async def emit_radar_watch_candidates(
    bot: SignalBot,
    store: object,
) -> dict[str, Any]:
    """Append ``radar_watch.jsonl``; optional operator DM when configured."""
    cfg = bot.settings.universe.radar
    rows = collect_radar_watch_rows(bot, store)
    summary: dict[str, Any] = {
        "emit_watch_candidates": bool(cfg.emit_watch_candidates),
        "candidates": len(rows),
        "logged": 0,
        "operator_dm_sent": 0,
    }
    if not rows:
        return summary

    telemetry = getattr(bot, "telemetry", None)
    for row in rows[: int(cfg.warm_pool_limit)]:
        if telemetry is not None:
            telemetry.append_jsonl("radar_watch.jsonl", row)
            summary["logged"] = int(summary["logged"]) + 1

    if not cfg.emit_watch_candidates:
        return summary

    if not operator_dm_enabled(bot, "send_radar_watch_candidate"):
        return summary

    dm_sent = 0
    for row in rows[:_MAX_RADAR_DM_PER_REFRESH]:
        sym = html.escape(str(row["symbol"]))
        flags = html.escape(", ".join(row.get("flags") or []))
        tier = html.escape(str(row.get("tier") or ""))
        text = (
            "<b>📡 Radar WATCH</b> (не ACTION)\n"
            f"{sym} · tier <code>{tier}</code>\n"
            f"Flags: <code>{flags}</code>\n"
            "<i>Кандидат воронки - не прошёл deep shortlist / delivery</i>"
        )
        try:
            if await send_operator_html(bot, text):
                dm_sent += 1
        except DEFENSIVE_EXC:
            LOG.debug("radar watch DM failed | symbol=%s", row.get("symbol"), exc_info=True)
    summary["operator_dm_sent"] = dm_sent
    if dm_sent:
        LOG.info("radar watch operator hints | sent=%d candidates=%d", dm_sent, len(rows))
    return summary

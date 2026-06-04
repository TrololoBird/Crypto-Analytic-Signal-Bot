"""WATCH → ACTION escalation hints for operator DMs (not auto channel publish)."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any

from bot.runtime.errors import DEFENSIVE_EXC
from bot.domain.delivery_policy import effective_action_min_score
from bot.domain.limit_entry import limit_delivery_ready, resolve_late_entry_chase_pct

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

    from bot.delivery.telegram_routing import operator_dm_enabled, send_operator_html

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
        "<i>Ручное решение — бот не эскалирует в канал автоматически</i>"
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

    from bot.delivery.ops_webhook import send_ops_webhook_alert

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

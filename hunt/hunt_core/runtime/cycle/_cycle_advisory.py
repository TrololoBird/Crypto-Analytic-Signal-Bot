"""Advisory TG helpers — liq burst, early alerts (cycle split)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from hunt_core.deliver.dispatch import mark_unified_sent, unified_cooldown_ok
from hunt_core.domain.config import COOLDOWN_MINUTES
from hunt_core.gate.delivery import delivery_hard_block
from hunt_core.market import HuntCcxtStreams
from hunt_core.runtime.state import LOG
# Legacy early/ignition/liquidation-burst advisory removed; the advisory senders below
# early-return, so these are inert stubs kept only to satisfy unreachable references.
def early_cooldown_ok(*_a, **_k) -> bool: return False
def early_telegram_block_reason(*_a, **_k) -> str: return "disabled"
def early_telegram_enabled(*_a, **_k) -> bool: return False
def evaluate_early_alert(*_a, **_k): return None
def format_early_telegram(*_a, **_k) -> str: return ""
def format_liquidation_burst_advisory(*_a, **_k) -> str: return ""
def liquidation_burst_from_streams(*_a, **_k): return None
def mark_early_sent(*_a, **_k) -> None: return None
from hunt_core.track.events import append_signal_event

async def _maybe_send_liq_burst_advisory(
    broadcaster: Any,
    *,
    symbol: str,
    ws_feed: HuntCcxtStreams | None,
    state: dict[str, str],
    now: datetime,
    send_telegram: bool,
) -> bool:
    """P1.9: liquidation cascade advisory — removed with the legacy early/advisory stack."""
    return False
    if not send_telegram or broadcaster is None or ws_feed is None:  # noqa: B027
        return False
    if os.getenv("HUNT_LIQ_BURST_TG", "0").strip().lower() not in {"1", "true", "yes"}:
        return False
    burst = liquidation_burst_from_streams(ws_feed, symbol)
    if burst is None:
        return False
    trade_dir = "short" if burst.direction == "dump" else "long"
    if not unified_cooldown_ok(
        state,
        symbol=symbol,
        direction=trade_dir,
        stage="early",
        now=now,
    ):
        return False
    liq_key = f"{symbol}:liq_burst"
    raw = state.get(liq_key)
    if raw:
        try:
            if now - datetime.fromisoformat(str(raw)) < timedelta(minutes=30):
                return False
        except ValueError:
            pass
    msg = format_liquidation_burst_advisory(burst)
    result = await broadcaster.send_html(msg)
    if result.status != "sent":
        LOG.warning(
            "hunt_liq_burst_telegram_failed",
            symbol=symbol,
            direction=burst.direction,
            status=result.status,
        )
        return False
    state[liq_key] = now.isoformat()
    mark_unified_sent(
        state,
        symbol=symbol,
        direction=trade_dir,
        stage="early",
        now=now,
    )
    append_signal_event(
        "liq_burst_advisory",
        symbol=symbol,
        direction=trade_dir,
        detail=burst.direction,
        payload={
            "notional_usd": burst.total_notional_usd,
            "events": burst.events,
            "score": burst.score,
        },
    )
    LOG.info(
        "hunt_liq_burst_telegram_sent",
        symbol=symbol,
        direction=burst.direction,
        notional=burst.total_notional_usd,
        events=burst.events,
    )
    return True


async def _maybe_send_early_alert(
    broadcaster: Any,
    *,
    symbol: str,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle_raw: Any,
    state: dict[str, str],
    mode: str,
    now: datetime,
) -> bool:
    """Prep/start Telegram before full closed-bar confirm."""
    if not early_telegram_enabled(symbol):
        return False
    early = evaluate_early_alert(
        setup,
        direction=direction,
        symbol=symbol,
        lifecycle=lifecycle_raw,
        row=row,
    )
    if early.kind in ("none", "confirm"):
        return False
    lc_phase = str((lifecycle_raw or {}).get("phase") or "")
    if (
        direction == "short"
        and mode not in ("short", "both")
        and lc_phase
        not in ("dump_active", "exhaustion_at_high", "distribution", "dump_initiating")
    ):
        return False
    if (
        direction == "long"
        and mode not in ("long", "both")
        and lc_phase
        not in (
            "post_dump_bounce",
            "accumulation",
            "recovery",
            "breakout_arming",
            "impulse_initiating",
        )
    ):
        return False
    block = early_telegram_block_reason(
        setup,
        direction=direction,
        lifecycle=lifecycle_raw,
        row=row,
        tier=early.tier,
    )
    if block:
        LOG.debug(
            "watch_early_telegram_blocked",
            symbol=symbol,
            direction=direction,
            tier=early.tier,
            reason=block,
        )
        return False
    if not early_cooldown_ok(symbol, direction, early.tier, state, now=now):
        return False
    if not unified_cooldown_ok(
        state, symbol=symbol, direction=direction, stage="early", now=now
    ):
        return False
    msg = format_early_telegram(
        row,
        direction=direction,
        setup=setup,
        lifecycle=lifecycle_raw,
        alert=early,
    )
    result = await broadcaster.send_html(msg)
    if result.status != "sent":
        LOG.warning(
            "watch_early_telegram_failed",
            symbol=symbol,
            direction=direction,
            tier=early.tier,
            status=result.status,
            reason=result.reason,
        )
        return False
    mark_early_sent(symbol, direction, early.tier, state, now=now)
    mark_unified_sent(state, symbol=symbol, direction=direction, stage="early", now=now)
    event_kind = {"prep": "prep", "imminent": "imminent", "start": "start"}.get(
        early.tier, "forming_early"
    )
    LOG.info(
        "watch_early_telegram_sent",
        symbol=symbol,
        direction=direction,
        tier=early.tier,
        message_id=result.message_id,
    )
    append_signal_event(
        event_kind,
        symbol=symbol,
        direction=direction,
        detail=early.message,
        payload={
            "tier": early.tier,
            "message_id": result.message_id,
            "fuel": setup.get("dump_fuel" if direction == "short" else "long_fuel"),
            "phase": setup.get("phase"),
            "lifecycle_phase": lc_phase,
        },
    )
    return True


def _cooldown_ok(
    symbol: str,
    direction: str,
    state: dict[str, str],
    *,
    now: datetime,
    minutes: int = COOLDOWN_MINUTES,
) -> bool:
    key = f"{symbol}:{direction}"
    raw = state.get(key) or state.get(symbol)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return now - last >= timedelta(minutes=minutes)


def _entry_past_tp1(setup: dict[str, Any], *, direction: str, price: float) -> bool:
    """Reject TG when price already at/through TP1 (hard stale only)."""
    return (
        delivery_hard_block(
            direction=direction,
            setup=setup,
            row={"price": price},
        )
        is not None
    )



__all__ = [
    "_cooldown_ok",
    "_entry_past_tp1",
    "_maybe_send_early_alert",
    "_maybe_send_liq_burst_advisory",
]

"""Active hunt signal tracking — invalidate, TP hit, phase change follow-ups."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

SignalEvent = Literal[
    "signal_open",
    "invalidate",
    "fix_profit_tp1",
    "fix_profit_tp2",
    "phase_change",
    "stop_warning",
    "avg_zone",
]

from hunt_watch.paths import SIGNAL_STATE as STATE_PATH

FOLLOWUP_COOLDOWN_MINUTES = 5
RECLAIM_BUFFER = 1.001  # 0.1% above invalidation before structural close


@dataclass(frozen=True, slots=True)
class HuntFollowUp:
    event: SignalEvent
    symbol: str
    direction: str
    message_key: str
    detail: str
    price: float
    payload: dict[str, Any]


def _key(symbol: str, direction: str) -> str:
    return f"{symbol.upper()}:{direction.lower()}"


def load_tracker_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"signals": {}, "followup_sent": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "signals" in raw:
            return raw
    except OSError, json.JSONDecodeError:
        pass
    return {"signals": {}, "followup_sent": {}}


def save_tracker_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _followup_allowed(state: dict[str, Any], message_key: str, *, now: datetime) -> bool:
    raw = (state.get("followup_sent") or {}).get(message_key)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    return now - last >= timedelta(minutes=FOLLOWUP_COOLDOWN_MINUTES)


def _mark_followup(state: dict[str, Any], message_key: str, *, now: datetime) -> None:
    sent = state.setdefault("followup_sent", {})
    sent[message_key] = now.isoformat()


def register_signal_open(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    price: float,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    now: datetime,
) -> None:
    k = _key(symbol, direction)
    ez = setup.get("entry_zone") or [price, price]
    state.setdefault("signals", {})[k] = {
        "status": "active",
        "opened_at": now.isoformat(),
        "direction": direction,
        "entry_lo": ez[0] if len(ez) > 0 else price,
        "entry_hi": ez[1] if len(ez) > 1 else price,
        "stop_loss": setup.get("stop_loss"),
        "tp1": setup.get("tp1"),
        "tp2": setup.get("tp2"),
        "phase": setup.get("phase"),
        "lifecycle_phase": (lifecycle or {}).get("phase"),
        "score": setup.get("dump_score") or setup.get("long_score"),
        "support_break_level": setup.get("support_break_level"),
        "invalidation_above": setup.get("invalidation_above"),
        "resistance_break_level": setup.get("resistance_break_level"),
        "invalidation_below": setup.get("invalidation_below"),
        "telegram_sent": bool(setup.get("telegram_sent")),
    }


def _short_structure_invalidated(
    active: dict[str, Any],
    setup: dict[str, Any],
    *,
    price: float,
) -> tuple[bool, str]:
    """Latch: do not cancel on score flicker — only structural breaks."""
    stop = float(active.get("stop_loss") or 0)
    if stop > 0 and price >= stop:
        return True, "stop_hit"
    reclaim = float(
        active.get("invalidation_above")
        or setup.get("invalidation_above")
        or active.get("support_break_level")
        or setup.get("support_break_level")
        or 0
    )
    if reclaim > 0 and price > reclaim * RECLAIM_BUFFER:
        return True, "reclaim_invalidation"
    return False, ""


def _long_structure_invalidated(
    active: dict[str, Any],
    setup: dict[str, Any],
    *,
    price: float,
) -> tuple[bool, str]:
    stop = float(active.get("stop_loss") or 0)
    if stop > 0 and price <= stop:
        return True, "stop_hit"
    break_below = float(
        active.get("invalidation_below")
        or setup.get("invalidation_below")
        or active.get("resistance_break_level")
        or setup.get("resistance_break_level")
        or 0
    )
    if break_below > 0 and price < break_below / RECLAIM_BUFFER:
        return True, "support_lost"
    return False, ""


def close_signal(state: dict[str, Any], *, symbol: str, direction: str) -> None:
    k = _key(symbol, direction)
    sig = (state.get("signals") or {}).get(k)
    if isinstance(sig, dict):
        sig["status"] = "closed"
        sig["closed_at"] = datetime.now(UTC).isoformat()


def evaluate_followups(
    state: dict[str, Any],
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[HuntFollowUp]:
    """Compare tick vs active signals; emit follow-up events (no entry cooldown)."""
    ts = now or datetime.now(UTC)
    events: list[HuntFollowUp] = []
    symbol = str(row.get("symbol") or "").upper()
    price = float(row.get("price") or 0)
    if not symbol or price <= 0:
        return events

    lifecycle = row.get("lifecycle") or {}
    lc_phase = str(lifecycle.get("phase") or "")

    for direction, setup_key in (("short", "dump"), ("long", "long")):
        setup = row.get(setup_key) or {}
        if not setup:
            continue
        k = _key(symbol, direction)
        active = (state.get("signals") or {}).get(k)
        if not active or active.get("status") != "active":
            continue

        if not active.get("telegram_sent"):
            continue

        tp1 = float(active.get("tp1") or 0)
        tp2 = float(active.get("tp2") or 0)
        stop = float(active.get("stop_loss") or 0)
        opened_phase = str(active.get("lifecycle_phase") or active.get("phase") or "")

        # Invalidate short on bounce / long on distribution
        if direction == "short" and lifecycle.get("invalidate_short"):
            msg_key = f"{k}:invalidate"
            if _followup_allowed(state, msg_key, now=ts):
                events.append(
                    HuntFollowUp(
                        event="invalidate",
                        symbol=symbol,
                        direction=direction,
                        message_key=msg_key,
                        detail=f"lifecycle={lc_phase}",
                        price=price,
                        payload={"reason": "bounce_invalidate", "phase": lc_phase},
                    )
                )
                close_signal(state, symbol=symbol, direction=direction)

        elif (
            direction == "long"
            and lc_phase
            in {
                "exhaustion_at_high",
                "distribution",
            }
            and opened_phase in {"post_dump_bounce", "accumulation", "recovery"}
        ):
            msg_key = f"{k}:invalidate"
            if _followup_allowed(state, msg_key, now=ts):
                events.append(
                    HuntFollowUp(
                        event="invalidate",
                        symbol=symbol,
                        direction=direction,
                        message_key=msg_key,
                        detail=f"phase={lc_phase}",
                        price=price,
                        payload={"reason": "trend_exhaustion", "phase": lc_phase},
                    )
                )
                close_signal(state, symbol=symbol, direction=direction)

        else:
            if direction == "short":
                struct_bad, struct_reason = _short_structure_invalidated(
                    active,
                    setup,
                    price=price,
                )
            else:
                struct_bad, struct_reason = _long_structure_invalidated(
                    active,
                    setup,
                    price=price,
                )
            if struct_bad:
                msg_key = f"{k}:invalidate:{struct_reason}"
                if _followup_allowed(state, msg_key, now=ts):
                    events.append(
                        HuntFollowUp(
                            event="invalidate",
                            symbol=symbol,
                            direction=direction,
                            message_key=msg_key,
                            detail=struct_reason,
                            price=price,
                            payload={
                                "reason": struct_reason,
                                "phase": setup.get("phase"),
                            },
                        )
                    )
                    close_signal(state, symbol=symbol, direction=direction)

        # Phase change while active
        if (
            active.get("status") == "active"
            and lc_phase
            and opened_phase
            and lc_phase != opened_phase
        ):
            msg_key = f"{k}:phase:{lc_phase}"
            if _followup_allowed(state, msg_key, now=ts):
                events.append(
                    HuntFollowUp(
                        event="phase_change",
                        symbol=symbol,
                        direction=direction,
                        message_key=msg_key,
                        detail=f"{opened_phase} → {lc_phase}",
                        price=price,
                        payload={"from": opened_phase, "to": lc_phase},
                    )
                )
                active["lifecycle_phase"] = lc_phase

        # TP hits
        if active.get("status") == "active":
            if direction == "short":
                if tp1 > 0 and price <= tp1:
                    msg_key = f"{k}:tp1"
                    if _followup_allowed(state, msg_key, now=ts):
                        events.append(
                            HuntFollowUp(
                                event="fix_profit_tp1",
                                symbol=symbol,
                                direction=direction,
                                message_key=msg_key,
                                detail=f"TP1 {_fmt(tp1)}",
                                price=price,
                                payload={"tp1": tp1},
                            )
                        )
                if tp2 > 0 and price <= tp2:
                    msg_key = f"{k}:tp2"
                    if _followup_allowed(state, msg_key, now=ts):
                        events.append(
                            HuntFollowUp(
                                event="fix_profit_tp2",
                                symbol=symbol,
                                direction=direction,
                                message_key=msg_key,
                                detail=f"TP2 {_fmt(tp2)}",
                                price=price,
                                payload={"tp2": tp2},
                            )
                        )
                        close_signal(state, symbol=symbol, direction=direction)
                if stop > 0 and price >= stop * 0.998:
                    msg_key = f"{k}:stop_warn"
                    if _followup_allowed(state, msg_key, now=ts):
                        events.append(
                            HuntFollowUp(
                                event="stop_warning",
                                symbol=symbol,
                                direction=direction,
                                message_key=msg_key,
                                detail=f"near SL {_fmt(stop)}",
                                price=price,
                                payload={"stop": stop},
                            )
                        )
            else:
                if tp1 > 0 and price >= tp1:
                    msg_key = f"{k}:tp1"
                    if _followup_allowed(state, msg_key, now=ts):
                        events.append(
                            HuntFollowUp(
                                event="fix_profit_tp1",
                                symbol=symbol,
                                direction=direction,
                                message_key=msg_key,
                                detail=f"TP1 {_fmt(tp1)}",
                                price=price,
                                payload={"tp1": tp1},
                            )
                        )
                if tp2 > 0 and price >= tp2:
                    msg_key = f"{k}:tp2"
                    if _followup_allowed(state, msg_key, now=ts):
                        events.append(
                            HuntFollowUp(
                                event="fix_profit_tp2",
                                symbol=symbol,
                                direction=direction,
                                message_key=msg_key,
                                detail=f"TP2 {_fmt(tp2)}",
                                price=price,
                                payload={"tp2": tp2},
                            )
                        )
                        close_signal(state, symbol=symbol, direction=direction)
                if stop > 0 and price <= stop * 1.002:
                    msg_key = f"{k}:stop_warn"
                    if _followup_allowed(state, msg_key, now=ts):
                        events.append(
                            HuntFollowUp(
                                event="stop_warning",
                                symbol=symbol,
                                direction=direction,
                                message_key=msg_key,
                                detail=f"near SL {_fmt(stop)}",
                                price=price,
                                payload={"stop": stop},
                            )
                        )

    return events


def mark_followups_sent(
    state: dict[str, Any], events: list[HuntFollowUp], *, now: datetime
) -> None:
    for ev in events:
        _mark_followup(state, ev.message_key, now=now)


def _fmt(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.3f}"
    if abs(value) >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"

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

from hunt_watch.param_store import tp1_partial_fix_pct as _tp1_pct
from hunt_watch.param_store import tracker_thresholds
from hunt_watch.paths import SIGNAL_HISTORY as HISTORY_PATH
from hunt_watch.paths import SIGNAL_STATE as STATE_PATH
from hunt_watch.signal_events import append_signal_event as _append_event

FOLLOWUP_COOLDOWN_MINUTES = 5
# No cosmetic phase_change TG right after entry (WLD: 2 flips in 60s post-confirm).
PHASE_CHANGE_GRACE_MIN = 20.0
RECLAIM_BUFFER = 1.001  # 0.1% above invalidation before structural close
# A hunt setup is a momentum trade — after this long without SL/TP it is stale.
SIGNAL_TIMEOUT_HOURS = 48.0
# Lifecycle contradicts open direction — auto-invalidate after N consecutive ticks.
STALE_LC_TICKS_DEFAULT = 3
_SHORT_STALE_PHASES = frozenset(
    {
        "no_setup",
        "post_dump_bounce",
        "recovery",
        "accumulation",
        "breakout_arming",
        "impulse_initiating",
    },
)
_LONG_STALE_PHASES = frozenset(
    {"distribution", "exhaustion_at_high", "dump_active"},
)


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
    entry_message_id: int | None = None,
) -> None:
    k = _key(symbol, direction)
    # One direction per symbol: a fresh confirmed opposite setup supersedes
    # the stale one (simultaneous SYM:long + SYM:short is a contradiction).
    opposite = _key(symbol, "long" if direction.lower() == "short" else "short")
    opp_sig = (state.get("signals") or {}).get(opposite)
    if isinstance(opp_sig, dict) and opp_sig.get("status") == "active":
        close_signal(
            state,
            symbol=symbol,
            direction="long" if direction.lower() == "short" else "short",
            reason="opposite_signal",
            exit_price=price,
            now=now,
        )
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
        "lifecycle_phase": (lifecycle or {}).get("phase") or setup.get("lifecycle_phase"),
        # immutable entry bucket — never updated by followups
        "entry_lifecycle_phase": (
            (lifecycle or {}).get("phase")
            or setup.get("lifecycle_phase")
            or setup.get("phase")
        ),
        "entry_lifecycle_bias": (lifecycle or {}).get("recommended_bias"),
        "lifecycle_bias": (lifecycle or {}).get("recommended_bias"),
        "score": setup.get("dump_score") or setup.get("long_score"),
        "fuel": setup.get("dump_fuel") or setup.get("long_fuel"),
        "support_break_level": setup.get("support_break_level"),
        "invalidation_above": setup.get("invalidation_above"),
        "resistance_break_level": setup.get("resistance_break_level"),
        "invalidation_below": setup.get("invalidation_below"),
        "telegram_sent": bool(setup.get("telegram_sent")),
        "entry_message_id": entry_message_id,
        "extreme_hi": price,
        "extreme_lo": price,
        "last_checked_at": now.isoformat(),
    }


def _worst_entry(active: dict[str, Any], *, direction: str) -> float:
    """Worst-case fill edge for R:R and breakeven SL."""
    if direction == "short":
        return float(active.get("entry_hi") or 0)
    return float(active.get("entry_lo") or 0)


def _mfe_pct(active: dict[str, Any], *, direction: str) -> float:
    """Max favorable excursion % from latched entry."""
    entry = _worst_entry(active, direction=direction)
    if entry <= 0:
        return 0.0
    if direction == "short":
        best = float(active.get("extreme_lo") or entry)
        return max(0.0, (entry - best) / entry * 100.0)
    best = float(active.get("extreme_hi") or entry)
    return max(0.0, (best - entry) / entry * 100.0)


def apply_tp1_management(
    active: dict[str, Any], *, direction: str, symbol: str = ""
) -> bool:
    """After TP1: partial fix (50% normal / 80% hot) + SL to entry."""
    if active.get("tp1_managed"):
        return False
    entry = _worst_entry(active, direction=direction)
    if entry <= 0:
        return False
    pct = _tp1_pct(symbol)
    if active.get("original_stop_loss") is None:
        active["original_stop_loss"] = active.get("stop_loss")
    buf = float(tracker_thresholds(symbol).get("breakeven_buffer_pct", 0.15)) / 100.0
    if direction == "short":
        be_stop = entry * (1.0 + buf)
    else:
        be_stop = entry * (1.0 - buf)
    active["stop_loss"] = round(be_stop, 6)
    active["partial_fixed_pct"] = pct
    active["sl_at_breakeven"] = True
    active["tp1_managed"] = True
    return True


def _latched_levels_payload(active: dict[str, Any]) -> dict[str, Any]:
    """Levels frozen at entry — follow-ups must not show live recalculated setup."""
    return {
        "stop_loss": active.get("stop_loss"),
        "tp1": active.get("tp1"),
        "tp2": active.get("tp2"),
        "entry_lo": active.get("entry_lo"),
        "entry_hi": active.get("entry_hi"),
        "opened_at": active.get("opened_at"),
        "entry_message_id": active.get("entry_message_id"),
        "score": active.get("score"),
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


def close_signal(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    reason: str = "manual",
    exit_price: float | None = None,
    now: datetime | None = None,
) -> None:
    """Terminal transition: always records outcome (reason / exit / pnl / duration)."""
    k = _key(symbol, direction)
    sig = (state.get("signals") or {}).get(k)
    if not isinstance(sig, dict) or sig.get("status") == "closed":
        return
    ts = now or datetime.now(UTC)
    sig["status"] = "closed"
    sig["closed_at"] = ts.isoformat()
    sig["close_reason"] = reason
    sig["close_lifecycle_phase"] = sig.get("lifecycle_phase")
    if exit_price is not None and exit_price > 0:
        sig["exit_price"] = exit_price
        lo = float(sig.get("entry_lo") or 0)
        hi = float(sig.get("entry_hi") or 0)
        mid = (lo + hi) / 2.0 if lo > 0 and hi > 0 else (lo or hi)
        if mid > 0:
            raw = (exit_price - mid) / mid * 100.0
            sig["pnl_pct"] = round(raw if direction == "long" else -raw, 2)
    try:
        opened = datetime.fromisoformat(str(sig.get("opened_at")))
        sig["duration_min"] = round((ts - opened).total_seconds() / 60.0, 1)
    except (TypeError, ValueError):
        pass
    # Snapshot MFE and TP1 progress at close for history/backtest analysis
    mfe = _mfe_pct(sig, direction=direction)
    sig["mfe_pct"] = round(mfe, 2)
    tp1 = float(sig.get("tp1") or 0)
    entry_edge = _worst_entry(sig, direction=direction)
    if tp1 > 0 and entry_edge > 0:
        tp1_dist = abs(entry_edge - tp1)
        if tp1_dist > 0:
            sig["tp1_progress_pct"] = round(min(mfe / tp1_dist * entry_edge, 100.0), 1)
    # Archive to closed_history so repeat signals on the same key don't lose prior outcomes
    history: list = state.setdefault("closed_history", [])
    record = dict(sig)
    record.setdefault("symbol", symbol)
    record.setdefault("direction", direction)
    history.append(record)
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open("a", encoding="utf-8") as _hf:
            _hf.write(json.dumps(record, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        _append_event(
            "close",
            symbol=symbol,
            direction=direction,
            detail=reason,
            payload={
                "close_reason": reason,
                "pnl_pct": sig.get("pnl_pct"),
                "duration_min": sig.get("duration_min"),
                "exit_price": sig.get("exit_price"),
                "close_lifecycle_phase": sig.get("close_lifecycle_phase"),
                "score": sig.get("score"),
                "fuel": sig.get("fuel"),
                "entry_lifecycle_phase": sig.get("entry_lifecycle_phase"),
                "entry_lifecycle_bias": sig.get("entry_lifecycle_bias"),
                "tp1_managed": sig.get("tp1_managed", False),
            },
        )
    except Exception:  # noqa: BLE001
        pass


# Minimum signal age before trusting wider bars for intrabar extremes: a live 5m
# candle may have opened BEFORE the signal did — its wick would falsely hit SL.
_BAR_MIN_AGE_MIN = {"1m": 0.0, "1m_closed": 2.0, "5m": 6.0, "5m_closed": 11.0}


def _entry_mid(active: dict[str, Any]) -> float:
    lo = float(active.get("entry_lo") or 0)
    hi = float(active.get("entry_hi") or 0)
    if lo > 0 and hi > 0:
        return (lo + hi) / 2.0
    return lo or hi


def _pnl_at_price(active: dict[str, Any], direction: str, price: float) -> float:
    mid = _entry_mid(active)
    if mid <= 0 or price <= 0:
        return 0.0
    raw = (price - mid) / mid * 100.0
    return raw if direction == "long" else -raw


def _signal_age_min(active: dict[str, Any], ts: datetime) -> float:
    try:
        opened = datetime.fromisoformat(str(active.get("opened_at")))
    except (TypeError, ValueError):
        return 0.0
    return (ts - opened).total_seconds() / 60.0


def _bar_extremes(
    row: dict[str, Any], active: dict[str, Any], *, price: float, ts: datetime
) -> tuple[float, float]:
    """Intrabar hi/lo since roughly the last poll — wicks must hit SL/TP, not only ticks."""
    hi = lo = price
    age = _signal_age_min(active, ts)
    timeframes = row.get("timeframes") or {}
    for tf_key, min_age in _BAR_MIN_AGE_MIN.items():
        if age < min_age:
            continue
        candle = (timeframes.get(tf_key) or {}).get("candle") or {}
        try:
            c_hi = float(candle.get("high") or 0)
            c_lo = float(candle.get("low") or 0)
        except (TypeError, ValueError):
            continue
        if c_hi > 0:
            hi = max(hi, c_hi)
        if c_lo > 0:
            lo = min(lo, c_lo)
    # Cumulative extremes across polls (kline reconcile also writes these).
    try:
        hi = max(hi, float(active.get("extreme_hi") or price))
        lo = min(lo, float(active.get("extreme_lo") or price))
    except (TypeError, ValueError):
        pass
    active["extreme_hi"] = hi
    active["extreme_lo"] = lo
    return hi, lo


def _stale_lifecycle_invalidate(
    state: dict[str, Any],
    active: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    lifecycle: dict[str, Any],
    row: dict[str, Any],
    price: float,
    ts: datetime,
    announced: bool,
) -> HuntFollowUp | None:
    """Close tracker position when lifecycle structurally contradicts the open thesis."""
    k = _key(symbol, direction)
    lc_phase = str(lifecycle.get("phase") or "")
    lc_bias = str(lifecycle.get("recommended_bias") or "")
    session = row.get("session") or {}
    pos = float(session.get("pos_in_range") or 0.5)

    contra = False
    ticks_needed = STALE_LC_TICKS_DEFAULT
    detail = ""

    if direction == "short":
        if lc_phase in _SHORT_STALE_PHASES:
            contra = True
            detail = f"lifecycle_stale:{lc_phase}"
            if lc_phase == "post_dump_bounce" and active.get("tp1_hit"):
                ticks_needed = 1
        elif lc_bias == "long":
            contra = True
            detail = f"lifecycle_stale:bias_long:{lc_phase}"
    else:
        if lc_phase in _LONG_STALE_PHASES:
            contra = True
            detail = f"lifecycle_stale:{lc_phase}"
            if lc_phase == "distribution" and pos >= 0.82:
                ticks_needed = 2

    if not contra:
        active["stale_lc_ticks"] = 0
        return None

    # Near-TP1 grace: if MFE is within 3% of TP1 distance, hold 8 ticks instead
    # of closing early. HUSDT/ARMUSDT were 1-2% from TP1 when stale fired at 3 ticks.
    if ticks_needed == STALE_LC_TICKS_DEFAULT and not active.get("tp1_hit"):
        tp1 = float(active.get("tp1") or 0)
        entry_lo = float(active.get("entry_lo") or 0)
        entry_hi = float(active.get("entry_hi") or 0)
        entry_mid = (entry_lo + entry_hi) / 2.0 if entry_lo and entry_hi else (entry_lo or entry_hi)
        if tp1 > 0 and entry_mid > 0:
            if direction == "short":
                tp1_dist = (entry_mid - tp1) / entry_mid * 100.0
                mfe = (entry_mid - float(active.get("extreme_lo") or entry_mid)) / entry_mid * 100.0
            else:
                tp1_dist = (tp1 - entry_mid) / entry_mid * 100.0
                mfe = (float(active.get("extreme_hi") or entry_mid) - entry_mid) / entry_mid * 100.0
            remaining = tp1_dist - mfe
            if 0 < remaining <= 3.0:
                ticks_needed = 8

    n = int(active.get("stale_lc_ticks") or 0) + 1
    active["stale_lc_ticks"] = n
    if n < ticks_needed:
        return None

    close_signal(
        state,
        symbol=symbol,
        direction=direction,
        reason="lifecycle_stale",
        exit_price=price,
        now=ts,
    )
    msg_key = f"{k}:invalidate:lifecycle_stale:{lc_phase}"
    if not _followup_allowed(state, msg_key, now=ts):
        return None
    return HuntFollowUp(
        event="invalidate",
        symbol=symbol,
        direction=direction,
        message_key=msg_key,
        detail=detail,
        price=price,
        payload={
            **_latched_levels_payload(active),
            "announced": announced,
            "reason": "lifecycle_stale",
            "phase": lc_phase,
            "stale_ticks": n,
            "pos_in_range": round(pos, 3),
        },
    )


def evaluate_levels(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    price: float,
    hi: float,
    lo: float,
    ts: datetime,
) -> list[HuntFollowUp]:
    """Latched SL/TP state machine against intrabar extremes.

    State transitions ALWAYS happen; the followup cooldown only dedupes
    messages. Transport flags (telegram_sent / entry_message_id) never gate
    state — they only mark events as announced for the sender.
    """
    events: list[HuntFollowUp] = []
    k = _key(symbol, direction)
    active = (state.get("signals") or {}).get(k)
    if not isinstance(active, dict) or active.get("status") != "active":
        return events
    announced = bool(active.get("telegram_sent")) or bool(active.get("entry_message_id"))

    tr = tracker_thresholds(symbol)
    stall_h = float(tr.get("mfe_stall_hours", 8.0))
    stall_min_mfe = float(tr.get("mfe_stall_min_pct", 1.0))
    age_min = _signal_age_min(active, ts)
    if (
        not active.get("tp1_hit")
        and age_min >= stall_h * 60.0
        and age_min < SIGNAL_TIMEOUT_HOURS * 60.0
        and _mfe_pct(active, direction=direction) < stall_min_mfe
    ):
        close_signal(
            state, symbol=symbol, direction=direction,
            reason="time_stall", exit_price=price, now=ts,
        )
        msg_key = f"{k}:invalidate:time_stall"
        if _followup_allowed(state, msg_key, now=ts):
            events.append(
                HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"time stall {stall_h:.0f}h · MFE "
                        f"{_mfe_pct(active, direction=direction):.1f}% < {stall_min_mfe:.1f}%"
                    ),
                    price=price,
                    payload={
                        **_latched_levels_payload(active),
                        "announced": announced,
                        "reason": "time_stall",
                    },
                )
            )
        return events

    if age_min >= SIGNAL_TIMEOUT_HOURS * 60.0:
        close_signal(
            state, symbol=symbol, direction=direction,
            reason="timeout", exit_price=price, now=ts,
        )
        msg_key = f"{k}:invalidate:timeout"
        if _followup_allowed(state, msg_key, now=ts):
            events.append(
                HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=f"timeout {SIGNAL_TIMEOUT_HOURS:.0f}h без SL/TP",
                    price=price,
                    payload={
                        **_latched_levels_payload(active),
                        "announced": announced,
                        "reason": "timeout",
                    },
                )
            )
        return events

    tp1 = float(active.get("tp1") or 0)
    tp2 = float(active.get("tp2") or 0)
    stop = float(active.get("stop_loss") or 0)
    latch = _latched_levels_payload(active)
    latch["announced"] = announced

    if active.get("tp1_hit") and not active.get("tp1_managed"):
        apply_tp1_management(active, direction=direction, symbol=symbol)
        latch = _latched_levels_payload(active)
        latch["announced"] = announced

    if direction == "short":
        stop_hit = stop > 0 and hi >= stop
        tp1_touch = tp1 > 0 and lo <= tp1
        tp2_touch = tp2 > 0 and lo <= tp2
        near_stop = stop > 0 and hi >= stop * 0.998
    else:
        stop_hit = stop > 0 and lo <= stop
        tp1_touch = tp1 > 0 and hi >= tp1
        tp2_touch = tp2 > 0 and hi >= tp2
        near_stop = stop > 0 and lo <= stop * 1.002

    # Stop first: a wick through SL ends the signal even if TP printed later.
    if stop_hit:
        close_signal(
            state, symbol=symbol, direction=direction,
            reason="stop_hit", exit_price=stop, now=ts,
        )
        msg_key = f"{k}:invalidate:stop_hit"
        if _followup_allowed(state, msg_key, now=ts):
            events.append(
                HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=f"SL {_fmt(stop)} пробит (intrabar)",
                    price=price,
                    payload={**latch, "reason": "stop_hit"},
                )
            )
        return events

    if tp2_touch:
        skipped = not active.get("tp1_hit")
        active["tp1_hit"] = True
        active["tp2_hit"] = True
        close_signal(
            state, symbol=symbol, direction=direction,
            reason="tp2", exit_price=tp2, now=ts,
        )
        msg_key = f"{k}:tp2"
        if _followup_allowed(state, msg_key, now=ts):
            detail = f"TP1+TP2 (пролёт) · TP2 {_fmt(tp2)}" if skipped else f"TP2 {_fmt(tp2)}"
            events.append(
                HuntFollowUp(
                    event="fix_profit_tp2",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=detail,
                    price=price,
                    payload={**latch, "tp2": tp2, "tp1_skipped": skipped},
                )
            )
        return events

    if tp1_touch and not active.get("tp1_hit"):
        active["tp1_hit"] = True
        apply_tp1_management(active, direction=direction, symbol=symbol)
        latch = {**_latched_levels_payload(active), "announced": announced, "tp1": tp1}
        entry = _worst_entry(active, direction=direction)
        fix_pct = int(active.get("partial_fixed_pct") or _tp1_pct(symbol))
        msg_key = f"{k}:tp1"
        if _followup_allowed(state, msg_key, now=ts):
            events.append(
                HuntFollowUp(
                    event="fix_profit_tp1",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"TP1 {_fmt(tp1)} · зафиксируй {fix_pct}% · "
                        f"SL → {_fmt(active.get('stop_loss'))} (BE+buf)"
                    ),
                    price=price,
                    payload={
                        **latch,
                        "partial_fixed_pct": fix_pct,
                        "sl_at_breakeven": True,
                    },
                )
            )

    if near_stop and not active.get("stop_warned"):
        active["stop_warned"] = True
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
                    payload={**latch, "stop": stop},
                )
            )
    return events


def latch_setup_if_active(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    setup: dict[str, Any],
) -> dict[str, Any]:
    """Keep confirmed=True while TG-active signal open — no demote next poll."""
    k = _key(symbol, direction)
    active = (state.get("signals") or {}).get(k)
    if not isinstance(active, dict) or active.get("status") != "active":
        return setup
    if not (active.get("telegram_sent") or active.get("entry_message_id")):
        return setup
    out = dict(setup)
    out["confirmed"] = True
    out["confirm_latched"] = True
    out["phase"] = "long_confirmed" if direction == "long" else "dump_confirmed"
    return out


def latch_row_setups(state: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Apply confirm latch to both setup sides on a watch row."""
    sym = str(row.get("symbol") or "")
    if not sym:
        return row
    for direction, key in (("short", "dump"), ("long", "long")):
        setup = row.get(key)
        if isinstance(setup, dict):
            row[key] = latch_setup_if_active(
                state, symbol=sym, direction=direction, setup=setup
            )
    return row


def _closed_adx_1h(row: dict[str, Any]) -> float | None:
    tf = row.get("timeframes") or {}
    block = tf.get("1h_closed") or tf.get("1h") or {}
    try:
        val = float(block.get("adx14") or 0)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


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
    lc_bias = str(lifecycle.get("recommended_bias") or "")

    for direction, setup_key in (("short", "dump"), ("long", "long")):
        setup = row.get(setup_key) or {}
        k = _key(symbol, direction)
        active = (state.get("signals") or {}).get(k)
        if not active or active.get("status") != "active":
            continue

        announced = bool(active.get("telegram_sent")) or bool(active.get("entry_message_id"))
        opened_phase = str(active.get("lifecycle_phase") or active.get("phase") or "")

        # 1) SL/TP against intrabar extremes — ALWAYS first, never skipped by
        # lifecycle branches and never gated by transport flags.
        hi, lo = _bar_extremes(row, active, price=price, ts=ts)
        events.extend(
            evaluate_levels(
                state, symbol=symbol, direction=direction,
                price=price, hi=hi, lo=lo, ts=ts,
            )
        )
        active["last_checked_at"] = ts.isoformat()
        if active.get("status") != "active":
            continue

        stale_fu = _stale_lifecycle_invalidate(
            state,
            active,
            symbol=symbol,
            direction=direction,
            lifecycle=lifecycle,
            row=row,
            price=price,
            ts=ts,
            announced=announced,
        )
        if stale_fu is not None:
            events.append(stale_fu)
            continue

        # 2) Lifecycle invalidation (bounce for shorts / exhaustion for longs).
        if direction == "short" and lifecycle.get("invalidate_short"):
            close_signal(
                state, symbol=symbol, direction=direction,
                reason="bounce_invalidate", exit_price=price, now=ts,
            )
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
                        payload={
                            **_latched_levels_payload(active),
                            "announced": announced,
                            "reason": "bounce_invalidate",
                            "phase": lc_phase,
                        },
                    )
                )

        elif (
            direction == "long"
            and lc_phase
            in {
                "exhaustion_at_high",
                "distribution",
            }
            and opened_phase in {
                "post_dump_bounce",
                "accumulation",
                "recovery",
                "breakout_arming",
                "impulse_initiating",
            }
        ):
            close_signal(
                state, symbol=symbol, direction=direction,
                reason="trend_exhaustion", exit_price=price, now=ts,
            )
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
                        payload={
                            **_latched_levels_payload(active),
                            "announced": announced,
                            "reason": "trend_exhaustion",
                            "phase": lc_phase,
                        },
                    )
                )

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
                close_signal(
                    state, symbol=symbol, direction=direction,
                    reason=struct_reason, exit_price=price, now=ts,
                )
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
                                **_latched_levels_payload(active),
                                "announced": announced,
                                "reason": struct_reason,
                                "phase": setup.get("phase"),
                            },
                        )
                    )

        # Bias change while active — TG only on COUNTER-bias flips (long→short etc).
        # wait↔long ping-pong and accumulation↔impulse renames stay silent;
        # material long thesis break uses stale_lc (dump_active × N ticks).
        opened_bias = str(
            active.get("entry_lifecycle_bias")
            or active.get("lifecycle_bias")
            or ""
        )
        if active.get("status") == "active" and lc_phase:
            active["lifecycle_phase"] = lc_phase
            if lc_bias:
                active["lifecycle_bias"] = lc_bias

        if (
            active.get("status") == "active"
            and lc_bias
            and opened_bias
            and lc_bias != opened_bias
            and _signal_age_min(active, ts) >= PHASE_CHANGE_GRACE_MIN
        ):
            counter_bias = "long" if direction == "short" else "short"
            if lc_bias == counter_bias:
                # HUSDT post-mortem: bias→long closed a losing short at -12.8%
                # while SL was never touched (wick missed between 60s polls).
                # Only crystallize on profitable flip or when SL is in play.
                stop = float(active.get("stop_loss") or 0)
                pnl_est = _pnl_at_price(active, direction, price)
                sl_in_play = (
                    direction == "short"
                    and stop > 0
                    and price >= stop * 0.998
                ) or (
                    direction == "long"
                    and stop > 0
                    and price <= stop * 1.002
                )
                if pnl_est < 0 and not sl_in_play:
                    msg_key = f"{k}:bias_warn:{lc_bias}"
                    if _followup_allowed(state, msg_key, now=ts):
                        events.append(
                            HuntFollowUp(
                                event="phase_change",
                                symbol=symbol,
                                direction=direction,
                                message_key=msg_key,
                                detail=(
                                    f"counter-bias {lc_bias} при PnL {pnl_est:+.1f}% "
                                    f"— держим до SL/TP ({opened_phase} → {lc_phase})"
                                ),
                                price=price,
                                payload={
                                    "from": opened_phase,
                                    "to": lc_phase,
                                    "bias_from": opened_bias,
                                    "bias_to": lc_bias,
                                    "announced": announced,
                                    "pnl_est": round(pnl_est, 2),
                                },
                            )
                        )
                    active["lifecycle_bias"] = lc_bias
                    continue
                if active.get("tp1_managed") or active.get("sl_at_breakeven"):
                    active["lifecycle_bias"] = lc_bias
                    continue
                # Q13: in chop (ADX<20) prefer fixed SL over bias-flip on winners.
                tr = tracker_thresholds(symbol)
                chop_adx = float(tr.get("bias_flip_chop_adx_max", 20.0))
                adx1h = _closed_adx_1h(row)
                if (
                    pnl_est >= 0
                    and adx1h is not None
                    and adx1h < chop_adx
                    and not sl_in_play
                ):
                    active["lifecycle_bias"] = lc_bias
                    continue
                close_signal(
                    state, symbol=symbol, direction=direction,
                    reason="bias_flip", exit_price=price, now=ts,
                )
                msg_key = f"{k}:invalidate:bias_flip"
                if _followup_allowed(state, msg_key, now=ts):
                    events.append(
                        HuntFollowUp(
                            event="invalidate",
                            symbol=symbol,
                            direction=direction,
                            message_key=msg_key,
                            detail=(
                                f"bias {opened_bias or '—'} → {lc_bias} "
                                f"({opened_phase} → {lc_phase})"
                            ),
                            price=price,
                            payload={
                                **_latched_levels_payload(active),
                                "announced": announced,
                                "reason": "bias_flip",
                                "bias_to": lc_bias,
                            },
                        )
                    )
                continue

    return events


def reconcile_signal(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    hi: float,
    lo: float,
    last_price: float,
    ts: datetime,
) -> list[HuntFollowUp]:
    """Orphan reconciliation: apply kline extremes fetched outside the watch loop.

    Used for active signals whose symbol is no longer in the watchlist —
    without this they never close (PLAYUSDT post-mortem: TP2 hit, stayed
    active for 18h).
    """
    active = (state.get("signals") or {}).get(_key(symbol, direction))
    if isinstance(active, dict) and active.get("status") == "active":
        active["extreme_hi"] = max(float(active.get("extreme_hi") or last_price), hi)
        active["extreme_lo"] = min(float(active.get("extreme_lo") or last_price), lo)
    events = evaluate_levels(
        state, symbol=symbol, direction=direction,
        price=last_price, hi=hi, lo=lo, ts=ts,
    )
    active = (state.get("signals") or {}).get(_key(symbol, direction))
    if isinstance(active, dict):
        active["last_checked_at"] = ts.isoformat()
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

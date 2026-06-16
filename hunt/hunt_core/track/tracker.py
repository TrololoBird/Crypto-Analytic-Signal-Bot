"""Active hunt signal tracking — invalidate, TP hit, phase change follow-ups."""
from __future__ import annotations



from hunt_core import clock
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Literal

SignalEvent = Literal[
    "signal_open",
    "invalidate",
    "fix_profit_tp1",
    "fix_profit_tp2",
    "phase_change",
    "entry_triggered",
    "stop_warning",
    "trailing_updated",
    "avg_zone",
]

from hunt_core.params.store import tp1_partial_fix_pct as _tp1_pct
from hunt_core.params.store import tracker_thresholds
from hunt_core.features.prepare_columns import feature_vector_from_row
from hunt_core.paths import SIGNAL_STATE as STATE_PATH
from hunt_core.track.events import append_signal_event as _append_event
from hunt_core.track.events import record_phase_transition as _record_phase_transition

_LOG = logging.getLogger(__name__)


class SignalPhase(str, Enum):
    REGISTERED = "registered"
    ARMED = "armed"
    TRIGGERED = "triggered"
    TP1_MANAGED = "tp1_managed"
    INVALIDATED = "invalidated"
    CLOSED = "closed"


_ACTIVE_PHASES = frozenset(
    {
        SignalPhase.REGISTERED,
        SignalPhase.ARMED,
        SignalPhase.TRIGGERED,
        SignalPhase.TP1_MANAGED,
    }
)
_ALLOWED_TRANSITIONS: dict[SignalPhase, frozenset[SignalPhase]] = {
    SignalPhase.REGISTERED: frozenset(
        {
            SignalPhase.ARMED,
            SignalPhase.TRIGGERED,
            SignalPhase.INVALIDATED,
            SignalPhase.CLOSED,
        }
    ),
    SignalPhase.ARMED: frozenset(
        {SignalPhase.TRIGGERED, SignalPhase.INVALIDATED, SignalPhase.CLOSED}
    ),
    SignalPhase.TRIGGERED: frozenset(
        {SignalPhase.TP1_MANAGED, SignalPhase.INVALIDATED, SignalPhase.CLOSED}
    ),
    SignalPhase.TP1_MANAGED: frozenset(
        {SignalPhase.INVALIDATED, SignalPhase.CLOSED}
    ),
    SignalPhase.INVALIDATED: frozenset({SignalPhase.CLOSED}),
    SignalPhase.CLOSED: frozenset(),
}
_INVALIDATING_CLOSE_REASONS = frozenset(
    {
        "stop_hit",
        "trailing_stop_profit",
        "bounce_invalidate",
        "trend_exhaustion",
        "reclaim_invalidation",
        "support_lost",
        "bias_flip",
        "lifecycle_stale",
        "orphan_expired",
        "time_stall",
        "timeout",
    }
)

FOLLOWUP_COOLDOWN_MINUTES = 5
# No cosmetic phase_change TG right after entry (WLD: 2 flips in 60s post-confirm).
PHASE_CHANGE_GRACE_MIN = 20.0
RECLAIM_BUFFER = 1.001  # fallback; prefer tracker_thresholds().reclaim_buffer
# A hunt setup is a momentum trade — after this long without SL/TP it is stale.
SIGNAL_TIMEOUT_HOURS = 48.0
# Phase 4A: level test tracking — approach within 0.3%, expire after 1.5×ATR reaction.
_LEVEL_APPROACH_TOLERANCE = 0.003
_LEVEL_REACTION_ATR_MULT = 1.5
# Phase 5B: BB squeeze + open profit → 30% tighter trail (volatility compression).
_SQUEEZE_TRAIL_TIGHTEN = 0.70
# H-A "sniper" hold-to-target exit (Gate G2, edge-validated 2026-06-12): on the live
# short slice the soft `lifecycle_stale` close forfeits winners — backtest on
# dump_active short (n=37) shows 19% SL / 43% reach TP2 when held to target/SL.
# So in sniper mode short positions ride to SL/TP (evaluate_levels) and structural
# invalidation (invalidate_short); the soft lifecycle_stale timeout is suppressed.
# The unit-tested `_stale_lifecycle_invalidate` itself is unchanged — gated at call site.
SNIPER_HOLD_TO_TARGET = os.environ.get("HUNT_SNIPER_MODE", "1") not in {"0", "false", "False"}
HUNT_EXIT_V2 = os.environ.get("HUNT_EXIT_V2", "").strip().lower() in {"1", "true", "yes"}
EXIT_V2_ACTIVE = HUNT_EXIT_V2 or SNIPER_HOLD_TO_TARGET
# Backward compat for logic_verify imports — runtime uses tracker_thresholds().
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


def _reclaim_buffer(symbol: str = "") -> float:
    return float(tracker_thresholds(symbol).get("reclaim_buffer", RECLAIM_BUFFER))


_DUMP_SHORT_ENTRY_PHASES = frozenset(
    {"dump_active", "distribution", "exhaustion_at_high"},
)
_BOUNCE_WITHIN_DUMP_PHASES = frozenset(
    {
        "impulse_initiating",
        "post_dump_bounce",
        "recovery",
        "accumulation",
        "breakout_arming",
    },
)


def _hold_short_through_dump_bounce(
    active: dict[str, Any],
    lifecycle: dict[str, Any],
    *,
    price: float,
    opened_bias: str,
    lc_bias: str,
    symbol: str = "",
) -> bool:
    """BEAT 2026-06-12: wait→long bounce closed a +EV short at 20m without entry reclaim."""
    if lc_bias != "long" or opened_bias not in {"wait", "short"}:
        return False
    entry_hi = float(active.get("entry_hi") or 0)
    if entry_hi <= 0 or price > entry_hi * _reclaim_buffer(symbol):
        return False
    opened_phase = str(active.get("entry_lifecycle_phase") or "")
    lc_phase = str(lifecycle.get("phase") or "")
    fall = float(lifecycle.get("fall_from_high_pct") or 0)
    dump_entry = opened_phase in _DUMP_SHORT_ENTRY_PHASES
    bounce_leg = lc_phase in _BOUNCE_WITHIN_DUMP_PHASES
    if dump_entry and bounce_leg and (fall >= 8.0 or opened_bias == "wait"):
        return True
    return opened_bias == "wait" and lc_bias == "long"


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


def _coerce_signal_phase(signal: dict[str, Any]) -> SignalPhase:
    """Resolve tracker FSM phase; infer from legacy rows when ``phase`` is absent."""
    raw = signal.get("phase")
    if isinstance(raw, SignalPhase):
        return raw
    if isinstance(raw, str) and raw in SignalPhase._value2member_map_:
        return SignalPhase(raw)
    if signal.get("status") == "closed":
        return SignalPhase.CLOSED
    if signal.get("tp1_managed"):
        return SignalPhase.TP1_MANAGED
    if str(signal.get("delivery_tier") or "") == "armed":
        return SignalPhase.ARMED
    if signal.get("status") == "active":
        return SignalPhase.TRIGGERED
    return SignalPhase.REGISTERED


def _sync_status_from_phase(signal: dict[str, Any]) -> None:
    phase = _coerce_signal_phase(signal)
    if phase in _ACTIVE_PHASES:
        signal["status"] = "active"
    elif phase in {SignalPhase.INVALIDATED, SignalPhase.CLOSED}:
        signal["status"] = "closed"


def _is_signal_active(signal: dict[str, Any]) -> bool:
    """Backward compat: ``status=='active'`` ⇔ phase ∈ {REGISTERED..TP1_MANAGED}."""
    phase = _coerce_signal_phase(signal)
    if phase in _ACTIVE_PHASES:
        return True
    if phase in {SignalPhase.INVALIDATED, SignalPhase.CLOSED}:
        return False
    return signal.get("status") == "active"


def _transition(
    signal: dict[str, Any],
    from_phase: SignalPhase | None,
    to_phase: SignalPhase,
    *,
    strict: bool = True,
) -> bool:
    current = _coerce_signal_phase(signal)
    if from_phase is not None and current != from_phase:
        if strict:
            _LOG.debug(
                "phase transition rejected %s -> %s (current=%s)",
                from_phase.value,
                to_phase.value,
                current.value,
            )
            return False
    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if to_phase not in allowed and to_phase != current:
        _LOG.debug(
            "phase transition not allowed %s -> %s",
            current.value,
            to_phase.value,
        )
        return False
    if to_phase == current:
        return True
    signal["phase"] = to_phase.value
    _sync_status_from_phase(signal)
    sym = str(signal.get("symbol") or "")
    direction = str(signal.get("direction") or "")
    if sym and direction:
        try:
            _record_phase_transition(
                symbol=sym,
                direction=direction,
                from_phase=current.value,
                to_phase=to_phase.value,
            )
        except Exception:  # noqa: BLE001
            pass
    return True


def _initial_signal_phase(setup: dict[str, Any]) -> SignalPhase:
    tier = str(setup.get("delivery_tier") or "triggered").lower()
    if tier == "armed":
        return SignalPhase.ARMED
    if tier == "triggered":
        return SignalPhase.TRIGGERED
    return SignalPhase.REGISTERED


def load_tracker_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"signals": {}, "followup_sent": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "signals" in raw:
            return raw
    except (OSError, json.JSONDecodeError):
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


def _close_already_notified(state: dict[str, Any], symbol: str, direction: str) -> bool:
    """True when terminal close/invalidate was already announced in Telegram."""
    k = _key(symbol, direction)
    sig = (state.get("signals") or {}).get(k)
    return isinstance(sig, dict) and bool(sig.get("close_notified"))


def mark_close_notified(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    message_key: str,
    now: datetime,
    remove_active: bool = True,
) -> None:
    """Latch terminal close TG — prevents re-close spam across ticks/processes."""
    k = _key(symbol, direction)
    sig = (state.get("signals") or {}).get(k)
    if not isinstance(sig, dict):
        return
    sig["close_notified"] = True
    sig["close_message_key"] = message_key
    sig["close_notified_at"] = now.isoformat()
    _mark_followup(state, message_key, now=now)
    if remove_active:
        state.setdefault("signals", {}).pop(k, None)


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
    features_open: dict[str, Any] | None = None,
    book_walls: dict[str, Any] | None = None,
) -> None:
    k = _key(symbol, direction)
    # One direction per symbol: a fresh confirmed opposite setup supersedes
    # the stale one (simultaneous SYM:long + SYM:short is a contradiction).
    opposite = _key(symbol, "long" if direction.lower() == "short" else "short")
    opp_sig = (state.get("signals") or {}).get(opposite)
    if isinstance(opp_sig, dict) and _is_signal_active(opp_sig):
        close_signal(
            state,
            symbol=symbol,
            direction="long" if direction.lower() == "short" else "short",
            reason="opposite_signal",
            exit_price=price,
            now=now,
        )
    ez = setup.get("entry_zone") or [price, price]
    initial_phase = _initial_signal_phase(setup)
    sig: dict[str, Any] = {
        "status": "active",
        "phase": initial_phase.value,
        "setup_phase": setup.get("phase"),
        "opened_at": now.isoformat(),
        "direction": direction,
        "entry_lo": ez[0] if len(ez) > 0 else price,
        "entry_hi": ez[1] if len(ez) > 1 else price,
        "stop_loss": setup.get("stop_loss"),
        "tp1": setup.get("tp1"),
        "tp2": setup.get("tp2"),
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
        "delivery_tier": setup.get("delivery_tier") or "triggered",
        "support_break_level": setup.get("support_break_level"),
        "invalidation_above": setup.get("invalidation_above"),
        "resistance_break_level": setup.get("resistance_break_level"),
        "invalidation_below": setup.get("invalidation_below"),
        "telegram_sent": bool(setup.get("telegram_sent")),
        "entry_message_id": entry_message_id,
        "extreme_hi": price,
        "extreme_lo": price,
        "last_checked_at": now.isoformat(),
        "last_reconcile_ts": now.isoformat(),
        "close_notified": False,
        "level_test_count": 0,
        "level_reaction_max_pct": 0.0,
        "level_expired": False,
    }
    if isinstance(features_open, dict):
        sig["features_open"] = features_open
    if isinstance(book_walls, dict):
        sig["book_walls"] = book_walls
    sig["symbol"] = symbol.upper()
    state.setdefault("signals", {})[k] = sig


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


def _squeeze_on_1h(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    tf = row.get("timeframes") or {}
    if not isinstance(tf, dict):
        return False
    block = tf.get("1h") or tf.get("1h_closed") or {}
    return bool(isinstance(block, dict) and block.get("squeeze_on"))


def _initial_risk_distance(active: dict[str, Any], *, direction: str) -> float:
    entry = _worst_entry(active, direction=direction)
    orig = float(active.get("original_stop_loss") or active.get("stop_loss") or 0)
    if entry <= 0 or orig <= 0:
        return 0.0
    if direction == "short" and orig > entry:
        return orig - entry
    if direction == "long" and orig < entry:
        return entry - orig
    return 0.0


def _stop_in_profit_zone(
    active: dict[str, Any], *, direction: str, stop: float
) -> bool:
    """True when SL sits beyond entry in the favorable direction (BE / trail lock)."""
    entry = _worst_entry(active, direction=direction)
    if entry <= 0 or stop <= 0:
        return False
    if direction == "short":
        return stop < entry
    return stop > entry


def _update_trailing_stop(
    active: dict[str, Any],
    *,
    direction: str,
    row: dict[str, Any] | None,
    symbol: str,
) -> tuple[bool, float]:
    """Trail SL behind peak MFE; squeeze_on + MFE>0 tightens room by 30% (Phase 5B).

    Returns ``(updated, previous_stop)`` for same-tick guards and TG notifications.
    """
    cur_stop = float(active.get("stop_loss") or 0)
    mfe = _mfe_pct(active, direction=direction)
    if mfe <= 0:
        return False, cur_stop
    initial_r = _initial_risk_distance(active, direction=direction)
    if initial_r <= 0:
        return False, cur_stop
    if active.get("original_stop_loss") is None:
        active["original_stop_loss"] = active.get("stop_loss")
    tr = tracker_thresholds(symbol)
    trail_frac = float(tr.get("breakeven_risk_fraction", 0.25))
    trail_dist = initial_r * trail_frac
    if _squeeze_on_1h(row):
        trail_dist *= _SQUEEZE_TRAIL_TIGHTEN
    entry = _worst_entry(active, direction=direction)
    if direction == "short":
        best = float(active.get("extreme_lo") or 0)
        if best <= 0:
            return False, cur_stop
        new_stop = best + trail_dist
        if new_stop >= entry or (cur_stop > 0 and new_stop >= cur_stop):
            return False, cur_stop
    else:
        best = float(active.get("extreme_hi") or 0)
        if best <= 0:
            return False, cur_stop
        new_stop = best - trail_dist
        if new_stop <= entry or (cur_stop > 0 and new_stop <= cur_stop):
            return False, cur_stop
    active["stop_loss"] = round(new_stop, 6)
    active["trailing_active"] = True
    # Once trailing SL is in profit territory, suppress bias_flip exits.
    if direction == "short" and new_stop < entry:
        active["sl_at_breakeven"] = True
    elif direction == "long" and new_stop > entry:
        active["sl_at_breakeven"] = True
    return True, cur_stop


def _tick_feature_latch(
    active: dict[str, Any],
    row: dict[str, Any],
    *,
    direction: str,
) -> None:
    """Update per-tick feature snapshots; latch peak when MFE improves."""
    active["features_last"] = feature_vector_from_row(row)
    cur_mfe = _mfe_pct(active, direction=direction)
    peak = float(active.get("peak_mfe_pct") or 0.0)
    if cur_mfe > peak + 0.001:
        active["peak_mfe_pct"] = round(cur_mfe, 2)
        active["features_peak"] = active["features_last"]


def apply_tp1_management(
    active: dict[str, Any], *, direction: str, symbol: str = ""
) -> bool:
    """After TP1: partial fix (50% normal / 80% hot) + lock the runner in profit.

    The post-TP1 stop must NEVER sit in the loss zone. The old logic placed it
    ``entry*(1+buf)`` with a 1% floor — i.e. >=1% *beyond entry on the adverse
    side* — which turned TP1 winners into ~1.5% losses (EPIC/UBU 2026-06-12)
    and clobbered an already profit-trailed stop (a +9%-locked trail reset to
    -1%). Instead lock a fraction of the realised TP1 distance: the stop sits
    between entry and TP1 — in profit, yet far enough from the entry-noise band
    that 1m wicks cannot reach it — and we never loosen a tighter trailed stop.
    """
    if active.get("tp1_managed"):
        return False
    entry = _worst_entry(active, direction=direction)
    if entry <= 0:
        return False
    pct = _tp1_pct(symbol)
    if active.get("original_stop_loss") is None:
        active["original_stop_loss"] = active.get("stop_loss")
    tp1 = float(active.get("tp1") or 0)
    lock_frac = float(tracker_thresholds(symbol).get("tp1_profit_lock_fraction", 0.5))
    cur = float(active.get("stop_loss") or 0)
    if direction == "short":
        gain = entry - tp1 if (0.0 < tp1 < entry) else 0.0
        lock_stop = min(entry - lock_frac * gain, entry)  # at/below entry = BE/profit
        if cur > 0:
            lock_stop = min(lock_stop, cur)  # never loosen a tighter trailed stop
    else:
        gain = tp1 - entry if tp1 > entry else 0.0
        lock_stop = max(entry + lock_frac * gain, entry)
        if cur > 0:
            lock_stop = max(lock_stop, cur)
    active["stop_loss"] = round(lock_stop, 6)
    active["partial_fixed_pct"] = pct
    active["sl_at_breakeven"] = True
    active["tp1_managed"] = True
    _transition(
        active,
        _coerce_signal_phase(active),
        SignalPhase.TP1_MANAGED,
        strict=False,
    )
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
    symbol: str = "",
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
    if break_below > 0 and price < break_below / _reclaim_buffer(symbol):
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
    archive: bool = True,
) -> None:
    """Terminal transition: always records outcome (reason / exit / pnl / duration).

    ``archive`` appends the closed record to the persistent ``signal_history.jsonl``.
    Tests pass ``archive=False`` so verify runs never pollute production data.
    """
    k = _key(symbol, direction)
    sig = (state.get("signals") or {}).get(k)
    if not isinstance(sig, dict) or _coerce_signal_phase(sig) == SignalPhase.CLOSED:
        return
    ts = now or clock.now_utc()
    cur = _coerce_signal_phase(sig)
    if reason in _INVALIDATING_CLOSE_REASONS and cur in _ACTIVE_PHASES:
        _transition(sig, cur, SignalPhase.INVALIDATED, strict=False)
        cur = _coerce_signal_phase(sig)
    _transition(sig, cur, SignalPhase.CLOSED, strict=False)
    sig["status"] = "closed"
    sig["closed_at"] = ts.isoformat()
    sig["close_reason"] = reason
    sig.setdefault("close_notified", False)
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
    if isinstance(sig.get("features_last"), dict):
        sig["features_close"] = sig["features_last"]
    sig.pop("features_last", None)
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
    if archive:
        try:
            from hunt_core.track.outcomes import append_outcome_record, kpi_bucket
            from hunt_core.paths import SIGNAL_HISTORY

            append_outcome_record(SIGNAL_HISTORY, {**record, "kpi_bucket": kpi_bucket(record)})
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
                "signal_phase": sig.get("phase"),
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


def duration_minutes(
    opened_at: str | None,
    *,
    now: datetime | None = None,
    end_at: str | None = None,
) -> float | None:
    """Minutes elapsed since ``opened_at`` (or until ``end_at`` when set)."""
    if not opened_at:
        return None
    try:
        start = datetime.fromisoformat(str(opened_at).replace(" ", "T"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end_at:
            end = datetime.fromisoformat(str(end_at).replace(" ", "T"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=UTC)
        else:
            end = now or clock.now_utc()
        return round((end - start).total_seconds() / 60.0, 1)
    except (TypeError, ValueError):
        return None


def _followup_trade_metrics(
    active: dict[str, Any],
    *,
    direction: str,
    price: float,
    ts: datetime,
) -> dict[str, Any]:
    """PnL % and duration for Telegram follow-ups."""
    return {
        "duration_min": duration_minutes(active.get("opened_at"), now=ts),
        "pnl_pct": round(_pnl_at_price(active, direction, price), 2),
    }


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
    archive: bool = True,
) -> HuntFollowUp | None:
    """Close tracker position when lifecycle structurally contradicts the open thesis.

    ``archive`` is threaded to the terminal ``close_signal`` so verify/test callers
    (``archive=False``) never append rows to the production ``signal_history.jsonl``.
    """
    if _close_already_notified(state, symbol, direction):
        return None
    k = _key(symbol, direction)
    lc_phase = str(lifecycle.get("phase") or "")
    lc_bias = str(lifecycle.get("recommended_bias") or "")
    session = row.get("session") or {}
    pos = float(session.get("pos_in_range") or 0.5)

    contra = False
    tr = tracker_thresholds(symbol)
    ticks_needed = int(tr.get("stale_lc_ticks_default", 3))
    near_tp1_ticks = int(tr.get("stale_lc_ticks_near_tp1", 8))
    near_tp1_pct = float(tr.get("near_tp1_remaining_pct", 3.0))
    detail = ""

    if direction == "short":
        opened_phase = str(
            active.get("entry_lifecycle_phase")
            or active.get("setup_phase")
            or active.get("phase")
            or ""
        )
        # Phase unchanged since entry — not a lifecycle transition (SPACEUSDT post-mortem:
        # short opened in impulse_initiating, stale fired 3 ticks later on same phase).
        if opened_phase and lc_phase == opened_phase:
            active["stale_lc_ticks"] = 0
            return None
        if active.get("tp1_managed") or active.get("tp1_hit") or active.get("sl_at_breakeven"):
            active["stale_lc_ticks"] = 0
            return None
        if lc_phase in _SHORT_STALE_PHASES:
            contra = True
            detail = f"lifecycle_stale:{lc_phase}"
            if lc_phase == "post_dump_bounce" and active.get("tp1_hit"):
                ticks_needed = 1
        elif lc_bias == "long":
            contra = True
            detail = f"lifecycle_stale:bias_long:{lc_phase}"
    else:
        opened_phase = str(
            active.get("entry_lifecycle_phase")
            or active.get("setup_phase")
            or active.get("phase")
            or ""
        )
        if opened_phase and lc_phase == opened_phase:
            active["stale_lc_ticks"] = 0
            return None
        if active.get("tp1_managed") or active.get("tp1_hit") or active.get("sl_at_breakeven"):
            active["stale_lc_ticks"] = 0
            return None
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
    if ticks_needed == int(tr.get("stale_lc_ticks_default", 3)) and not active.get("tp1_hit"):
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
            if 0 < remaining <= near_tp1_pct:
                ticks_needed = near_tp1_ticks

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
        archive=archive,
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
            **_followup_trade_metrics(active, direction=direction, price=price, ts=ts),
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
    row: dict[str, Any] | None = None,
) -> list[HuntFollowUp]:
    """Latched SL/TP state machine against intrabar extremes.

    State transitions ALWAYS happen; the followup cooldown only dedupes
    messages. Transport flags (telegram_sent / entry_message_id) never gate
    state — they only mark events as announced for the sender.
    """
    events: list[HuntFollowUp] = []
    k = _key(symbol, direction)
    active = (state.get("signals") or {}).get(k)
    if not isinstance(active, dict) or not _is_signal_active(active):
        return events
    if _close_already_notified(state, symbol, direction):
        return events
    announced = bool(active.get("telegram_sent")) or bool(active.get("entry_message_id"))

    tr = tracker_thresholds(symbol)
    orphan_ttl_h = float(tr.get("orphan_ttl_hours", 24.0))
    last_rec_raw = active.get("last_reconcile_ts") or active.get("opened_at")
    try:
        last_rec = datetime.fromisoformat(str(last_rec_raw))
        if last_rec.tzinfo is None:
            last_rec = last_rec.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        last_rec = ts
    orphan_age_h = (ts - last_rec).total_seconds() / 3600.0
    if orphan_age_h >= orphan_ttl_h:
        _LOG.warning(
            "orphan_expired %s:%s — last reconcile %.1fh ago (ttl=%.0fh)",
            symbol,
            direction,
            orphan_age_h,
            orphan_ttl_h,
        )
        close_signal(
            state,
            symbol=symbol,
            direction=direction,
            reason="orphan_expired",
            exit_price=price,
            now=ts,
        )
        msg_key = f"{k}:invalidate:orphan_expired"
        if _followup_allowed(state, msg_key, now=ts):
            events.append(
                HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"orphan TTL {orphan_ttl_h:.0f}h · "
                        f"last reconcile {orphan_age_h:.1f}h ago"
                    ),
                    price=price,
                    payload={
                        **_latched_levels_payload(active),
                        "announced": announced,
                        "reason": "orphan_expired",
                        **_followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )
        return events

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
                        **_followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
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
                        **_followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )
        return events

    trail_updated, prev_stop = _update_trailing_stop(
        active, direction=direction, row=row, symbol=symbol
    )

    tp1 = float(active.get("tp1") or 0)
    tp2 = float(active.get("tp2") or 0)
    stop = float(active.get("stop_loss") or 0)
    latch = _latched_levels_payload(active)
    latch["announced"] = announced

    if trail_updated and stop > 0:
        protected = round(_pnl_at_price(active, direction, stop), 2)
        msg_key = f"{k}:trailing:{stop:.6f}"
        if _followup_allowed(state, msg_key, now=ts):
            events.append(
                HuntFollowUp(
                    event="trailing_updated",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=(
                        f"Trailing SL → {_fmt(stop)} · защита ~{protected:.1f}%"
                    ),
                    price=price,
                    payload={
                        **latch,
                        "stop_loss": stop,
                        "prev_stop": prev_stop,
                        "protected_pnl_pct": protected,
                        "trailing_active": True,
                        **_followup_trade_metrics(
                            active, direction=direction, price=price, ts=ts
                        ),
                    },
                )
            )

    if active.get("tp1_hit") and not active.get("tp1_managed"):
        apply_tp1_management(active, direction=direction, symbol=symbol)
        latch = _latched_levels_payload(active)
        latch["announced"] = announced
        stop = float(active.get("stop_loss") or 0)

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

    # Same-tick guard: trailing into profit zone must not instant-close on stale hi/lo.
    if (
        stop_hit
        and trail_updated
        and _stop_in_profit_zone(active, direction=direction, stop=stop)
    ):
        stop_hit = False

    # Stop first: a wick through SL ends the signal even if TP printed later.
    if stop_hit:
        pnl_at_stop = _pnl_at_price(active, direction, stop)
        if active.get("trailing_active") and pnl_at_stop > 0:
            close_reason = "trailing_stop_profit"
            detail_msg = (
                f"Trailing SL {_fmt(stop)} · фиксация +{pnl_at_stop:.1f}%"
            )
        else:
            close_reason = "stop_hit"
            detail_msg = f"SL {_fmt(stop)} пробит (intrabar)"
        _transition(
            active,
            _coerce_signal_phase(active),
            SignalPhase.INVALIDATED,
            strict=False,
        )
        close_signal(
            state, symbol=symbol, direction=direction,
            reason=close_reason, exit_price=stop, now=ts,
        )
        msg_key = f"{k}:invalidate:{close_reason}"
        if _followup_allowed(state, msg_key, now=ts):
            events.append(
                HuntFollowUp(
                    event="invalidate",
                    symbol=symbol,
                    direction=direction,
                    message_key=msg_key,
                    detail=detail_msg,
                    price=price,
                    payload={
                        **latch,
                        "reason": close_reason,
                        "trailing_active": bool(active.get("trailing_active")),
                        **_followup_trade_metrics(
                            active, direction=direction, price=stop, ts=ts
                        ),
                    },
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
        _worst_entry(active, direction=direction)
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
                        **_followup_trade_metrics(
                            active, direction=direction, price=tp1, ts=ts
                        ),
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
    if not isinstance(active, dict) or not _is_signal_active(active):
        return setup
    if not (active.get("telegram_sent") or active.get("entry_message_id")):
        return setup
    out = dict(setup)
    out["confirmed"] = True
    out["confirm_latched"] = True
    out["phase"] = "long_confirmed" if direction == "long" else "dump_confirmed"
    if active.get("level_expired"):
        out["level_expired"] = True
        out["level_test_count"] = active.get("level_test_count")
        out["level_reaction_max_pct"] = active.get("level_reaction_max_pct")
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


def _closed_atr1h_pct(row: dict[str, Any]) -> float:
    tf = row.get("timeframes") or {}
    block = tf.get("1h_closed") or tf.get("1h") or {}
    try:
        val = float(block.get("atr_pct") or 0)
    except (TypeError, ValueError):
        return 0.0
    return val if val > 0 else 0.0


def _tracked_level(
    active: dict[str, Any],
    setup: dict[str, Any],
    *,
    direction: str,
) -> float:
    latched = float(active.get("track_level") or 0)
    if latched > 0:
        return latched
    if direction == "short":
        return float(
            active.get("support_break_level")
            or setup.get("support_break_level")
            or active.get("entry_lo")
            or setup.get("invalidation_above")
            or 0
        )
    return float(
        active.get("resistance_break_level")
        or setup.get("resistance_break_level")
        or active.get("entry_hi")
        or setup.get("invalidation_below")
        or 0
    )


def _update_level_test_tracking(
    active: dict[str, Any],
    setup: dict[str, Any],
    row: dict[str, Any],
    *,
    direction: str,
    price: float,
) -> None:
    """Detect approach+bounce at latched level; expire after reaction >= 1.5×ATR."""
    if active.get("level_expired") or price <= 0:
        return
    level = _tracked_level(active, setup, direction=direction)
    if level <= 0:
        return
    active["track_level"] = level

    tol = level * _LEVEL_APPROACH_TOLERANCE
    dist = abs(price - level)
    in_zone = dist <= tol
    atr_pct = _closed_atr1h_pct(row)
    reaction_floor = max(0.5, _LEVEL_REACTION_ATR_MULT * max(atr_pct, 0.5))

    approaching = bool(active.get("_level_approaching"))
    if in_zone and not approaching:
        active["_level_approaching"] = True
        active["_level_touch_extreme"] = price
        return

    if approaching:
        extreme = float(active.get("_level_touch_extreme") or price)
        if direction == "short":
            extreme = min(extreme, price)
            reaction_pct = max(0.0, (price - extreme) / level * 100.0)
        else:
            extreme = max(extreme, price)
            reaction_pct = max(0.0, (extreme - price) / level * 100.0)
        active["_level_touch_extreme"] = extreme

        peak = float(active.get("level_reaction_max_pct") or 0.0)
        if reaction_pct > peak:
            active["level_reaction_max_pct"] = round(reaction_pct, 3)

        if reaction_pct >= reaction_floor:
            active["level_expired"] = True
            active.pop("_level_approaching", None)
            active.pop("_level_touch_extreme", None)
            return

        if not in_zone and reaction_pct >= reaction_floor * 0.35:
            active["level_test_count"] = int(active.get("level_test_count") or 0) + 1
            active["_level_approaching"] = False
            active.pop("_level_touch_extreme", None)
    elif not in_zone:
        active.pop("_level_approaching", None)
        active.pop("_level_touch_extreme", None)


def _maybe_armed_to_triggered(
    state: dict[str, Any],
    active: dict[str, Any],
    *,
    setup: dict[str, Any],
    symbol: str,
    direction: str,
    price: float,
    ts: datetime,
    announced: bool,
) -> HuntFollowUp | None:
    """ARMED setup → TRIGGERED when price enters the latched entry zone."""
    if active.get("delivery_tier") != "armed":
        return None
    from hunt_core.gate.delivery import price_in_entry_zone  # noqa: PLC0415

    if not price_in_entry_zone(
        {
            "entry_zone": [active.get("entry_lo"), active.get("entry_hi")],
        },
        price,
        direction=direction,
    ):
        return None
    k = _key(symbol, direction)
    msg_key = f"{k}:entry_triggered"
    if not _followup_allowed(state, msg_key, now=ts):
        return None
    active["delivery_tier"] = "triggered"
    _transition(active, SignalPhase.ARMED, SignalPhase.TRIGGERED, strict=False)
    return HuntFollowUp(
        event="entry_triggered",
        symbol=symbol,
        direction=direction,
        message_key=msg_key,
        detail="price_in_entry_zone",
        price=price,
        payload={
            **_latched_levels_payload(active),
            "announced": announced,
            "reason": "entry_triggered",
        },
    )


def evaluate_followups(
    state: dict[str, Any],
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[HuntFollowUp]:
    """Compare tick vs active signals; emit follow-up events (no entry cooldown)."""
    ts = now or clock.now_utc()
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
        if not active or not _is_signal_active(active):
            continue

        announced = bool(active.get("telegram_sent")) or bool(active.get("entry_message_id"))
        opened_phase = str(
            active.get("lifecycle_phase")
            or active.get("setup_phase")
            or active.get("phase")
            or ""
        )

        # 1) SL/TP against intrabar extremes — ALWAYS first, never skipped by
        # lifecycle branches and never gated by transport flags.
        hi, lo = _bar_extremes(row, active, price=price, ts=ts)
        _tick_feature_latch(active, row, direction=direction)
        _update_level_test_tracking(
            active, setup, row, direction=direction, price=price
        )
        events.extend(
            evaluate_levels(
                state, symbol=symbol, direction=direction,
                price=price, hi=hi, lo=lo, ts=ts, row=row,
            )
        )
        active["last_checked_at"] = ts.isoformat()
        if _is_signal_active(active):
            active["last_reconcile_ts"] = ts.isoformat()
        if not _is_signal_active(active):
            continue

        armed_fu = _maybe_armed_to_triggered(
            state,
            active,
            setup=setup,
            symbol=symbol,
            direction=direction,
            price=price,
            ts=ts,
            announced=announced,
        )
        if armed_fu is not None:
            events.append(armed_fu)

        stale_fu = None
        if not (EXIT_V2_ACTIVE and direction == "short"):
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
        bounce_invalidate = bool(lifecycle.get("invalidate_short")) and direction == "short"
        if bounce_invalidate:
            tr = tracker_thresholds(symbol)
            bounce_grace = float(tr.get("bounce_invalidate_grace_min", 15.0))
            entry_hi = float(active.get("entry_hi") or 0)
            age_min = _signal_age_min(active, ts)
            # PLAYUSDT: +2.9% wick in 2m while still below entry_hi — not a reclaim.
            if age_min < bounce_grace or (entry_hi > 0 and price <= entry_hi):
                bounce_invalidate = False
        if bounce_invalidate:
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
                            **_followup_trade_metrics(
                                active, direction=direction, price=price, ts=ts
                            ),
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
                            **_followup_trade_metrics(
                                active, direction=direction, price=price, ts=ts
                            ),
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
                    symbol=symbol,
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
                                **_followup_trade_metrics(
                                    active, direction=direction, price=price, ts=ts
                                ),
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
        if _is_signal_active(active) and lc_phase:
            active["lifecycle_phase"] = lc_phase
            if lc_bias:
                active["lifecycle_bias"] = lc_bias

        if (
            _is_signal_active(active)
            and lc_bias
            and opened_bias
            and lc_bias != opened_bias
            and _signal_age_min(active, ts) >= PHASE_CHANGE_GRACE_MIN
        ):
            counter_bias = "long" if direction == "short" else "short"
            if lc_bias == counter_bias:
                if direction == "short" and _hold_short_through_dump_bounce(
                    active,
                    lifecycle,
                    price=price,
                    opened_bias=opened_bias,
                    lc_bias=lc_bias,
                    symbol=symbol,
                ):
                    active["lifecycle_bias"] = lc_bias
                    continue
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
                                **_followup_trade_metrics(
                                    active, direction=direction, price=price, ts=ts
                                ),
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
    if isinstance(active, dict) and _is_signal_active(active):
        active["extreme_hi"] = max(float(active.get("extreme_hi") or last_price), hi)
        active["extreme_lo"] = min(float(active.get("extreme_lo") or last_price), lo)
    events = evaluate_levels(
        state, symbol=symbol, direction=direction,
        price=last_price, hi=hi, lo=lo, ts=ts,
    )
    active = (state.get("signals") or {}).get(_key(symbol, direction))
    if isinstance(active, dict):
        active["last_checked_at"] = ts.isoformat()
        if _is_signal_active(active):
            active["last_reconcile_ts"] = ts.isoformat()
    return events


def mark_followups_sent(
    state: dict[str, Any], events: list[HuntFollowUp], *, now: datetime
) -> None:
    for ev in events:
        _mark_followup(state, ev.message_key, now=now)


def reconcile_orphan(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    hi: float,
    lo: float,
    last_price: float,
    ts: datetime,
) -> list[HuntFollowUp]:
    """Reconcile one active signal against kline extremes."""
    return reconcile_signal(
        state,
        symbol=symbol,
        direction=direction,
        hi=hi,
        lo=lo,
        last_price=last_price,
        ts=ts,
    )


def _fmt(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.3f}"
    if abs(value) >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"

"""Signal lifecycle — setup_id dedup, state transitions, cooldown store."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from hunt_core.paths import SESSION_DIR
from hunt_core.signals.model import Signal, SignalModule, SignalState

_LOG = logging.getLogger(__name__)

_COOLDOWN_HOURS = 4.0
_STORE_PATH = SESSION_DIR / "signal_lifecycle.json"


def _round_anchor(price: float) -> float:
    """Tick-fraction anchor — stable across minor price drift."""
    if price <= 0:
        return 0.0
    if price >= 1000:
        return round(price, 1)
    if price >= 10:
        return round(price, 2)
    return round(price, 4)


def compute_setup_id(
    *,
    thesis_kind: str,
    anchor_level: float,
    direction: str,
) -> str:
    """Stable dedup key — NOT price-derived entry/sl every tick."""
    payload = {
        "thesis": str(thesis_kind or "unknown"),
        "anchor": _round_anchor(float(anchor_level or 0)),
        "direction": str(direction or "").lower(),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return digest[:16]


def _thesis_from_row(row: dict[str, Any], summary: dict[str, Any]) -> tuple[str, str, float]:
    direction = str(summary.get("action") or "wait").lower()
    v2 = row.get("verdict_v2")
    catalyst = getattr(v2, "catalyst", None) if v2 else None
    path = getattr(v2, "expected_path", None) if v2 else None
    thesis_kind = ""
    if catalyst is not None:
        thesis_kind = str(getattr(catalyst, "primary", "") or getattr(catalyst, "label", ""))
    if not thesis_kind and path is not None:
        thesis_kind = str(getattr(path, "type", "") or "")
    anchor = summary.get("catalyst_level")
    if anchor is None:
        lo = float(summary.get("entry_lo") or 0)
        hi = float(summary.get("entry_hi") or 0)
        anchor = (lo + hi) / 2 if lo > 0 and hi > 0 else float(row.get("price") or 0)
    try:
        anchor_f = float(anchor)
    except (TypeError, ValueError):
        anchor_f = float(row.get("price") or 0)
    thesis = str(summary.get("path") or thesis_kind or direction)
    return direction, thesis_kind or thesis, anchor_f


def _plan_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_lo": summary.get("entry_lo"),
        "entry_hi": summary.get("entry_hi"),
        "stop_loss": summary.get("stop_loss"),
        "tp1": summary.get("tp1"),
        "tp2": summary.get("tp2"),
        "tp3": summary.get("tp3"),
        "rr_primary": summary.get("rr_primary"),
        "rr_base_label": summary.get("rr_base_label"),
        "catalyst_level": summary.get("catalyst_level"),
    }


@dataclass
class LifecycleTransition:
    event: Literal["signal", "activated", "none"]
    signal: Signal | None = None
    suppress_reason: str = ""


@dataclass
class SignalLifecycleStore:
    """Per-setup_id cooldown + last emitted state."""

    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path=_STORE_PATH) -> SignalLifecycleStore:
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        entries = raw.get("entries") if isinstance(raw.get("entries"), dict) else raw
        return cls(entries=dict(entries or {}))

    def save(self, path=_STORE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"entries": self.entries, "updated_at": datetime.now(UTC).isoformat()}, indent=2),
            encoding="utf-8",
        )

    def _cooldown_ok(self, setup_id: str, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        entry = self.entries.get(setup_id)
        if not entry:
            return True
        last = entry.get("last_emit_at")
        if not last:
            return True
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return True
        return now - dt >= timedelta(hours=_COOLDOWN_HOURS)

    def record_emit(self, signal: Signal, *, event: str) -> None:
        self.entries[signal.setup_id] = {
            "symbol": signal.symbol,
            "direction": signal.direction,
            "state": signal.state,
            "last_event": event,
            "last_emit_at": datetime.now(UTC).isoformat(),
            "module": signal.module,
        }

    def last_state(self, setup_id: str) -> str:
        entry = self.entries.get(setup_id) or {}
        return str(entry.get("state") or "")


def process_lifecycle_tick(
    row: dict[str, Any],
    *,
    module: SignalModule = 1,
    store: SignalLifecycleStore | None = None,
    commit: bool = True,
) -> LifecycleTransition:
    """Evaluate one tick — emit only on real setup state advance; WAIT → silence."""
    summary = row.get("verdict_v2_summary") if isinstance(row.get("verdict_v2_summary"), dict) else {}
    action = str(summary.get("action") or "wait").lower()
    sym = str(row.get("symbol") or "").upper()
    as_of = str(row.get("as_of") or row.get("ts") or datetime.now(UTC).isoformat())

    if action not in {"long", "short"}:
        return LifecycleTransition(event="none", suppress_reason="wait_or_no_setup")

    from hunt_core.signals.price_sanity import price_sanity_check

    ok_price, price_reason = price_sanity_check(row)
    if not ok_price:
        return LifecycleTransition(event="none", suppress_reason=f"price_sanity:{price_reason}")

    direction, thesis_kind, anchor = _thesis_from_row(row, summary)
    setup_id = compute_setup_id(thesis_kind=thesis_kind, anchor_level=anchor, direction=direction)
    store = store or SignalLifecycleStore.load()

    activation = str(summary.get("activation") or "")
    prev_state = store.last_state(setup_id)
    now_state: SignalState = "forming"
    event: Literal["signal", "activated", "none"] = "none"

    if activation == "in_entry_zone":
        now_state = "activated"
        if prev_state != "activated":
            event = "activated"
    elif activation in {"near_entry", "near_catalyst", "at_catalyst"}:
        now_state = "signal"
        if prev_state not in {"signal", "activated", "tracking"}:
            event = "signal"
    else:
        now_state = "signal"
        if prev_state not in {"signal", "activated", "tracking"}:
            event = "signal"

    if event == "none":
        return LifecycleTransition(event="none", suppress_reason="no_state_advance")

    if not store._cooldown_ok(setup_id) and event == "signal" and prev_state == "signal":
        return LifecycleTransition(event="none", suppress_reason="setup_cooldown")

    plan = _plan_from_summary(summary)
    signal = Signal(
        symbol=sym,
        module=module,
        direction=direction,
        setup_id=setup_id,
        thesis=thesis_kind,
        plan=plan,
        state=now_state,
        created_at=as_of,
        activated_at=as_of if event == "activated" else "",
        as_of=as_of,
        provenance={"path": summary.get("path"), "strength": summary.get("strength")},
    )
    if commit:
        store.record_emit(signal, event=event)
        store.save()
    return LifecycleTransition(event=event, signal=signal)


def build_scanner_signal(
    *,
    symbol: str,
    direction: str,
    setup_id: str,
    thesis: str,
    plan: dict[str, Any],
    as_of: str,
    store: SignalLifecycleStore | None = None,
) -> LifecycleTransition:
    """Scanner Module 2 — emit when energy+direction resolve (pre-move)."""
    store = store or SignalLifecycleStore.load()
    sid = setup_id or compute_setup_id(
        thesis_kind=thesis,
        anchor_level=float(plan.get("trigger_level") or plan.get("entry_lo") or 0),
        direction=direction,
    )
    prev = store.last_state(sid)
    if prev in {"signal", "activated", "tracking"}:
        return LifecycleTransition(event="none", suppress_reason="already_emitted")
    if not store._cooldown_ok(sid):
        return LifecycleTransition(event="none", suppress_reason="setup_cooldown")
    signal = Signal(
        symbol=str(symbol).upper(),
        module=2,
        direction=direction.lower(),
        setup_id=sid,
        thesis=thesis,
        plan=plan,
        state="signal",
        created_at=as_of,
        as_of=as_of,
        provenance={"module": "scanner"},
    )
    store.record_emit(signal, event="signal")
    store.save()
    return LifecycleTransition(event="signal", signal=signal)

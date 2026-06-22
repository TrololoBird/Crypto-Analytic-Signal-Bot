"""Confirm delivery suppression and blocked-telemetry helpers (cycle split)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from hunt_core.track.events import append_signal_event
from hunt_core.deliver.dispatch import unified_cooldown_ok

import structlog

LOG = structlog.get_logger(__name__)

def _advisory_tg_enabled() -> bool:
    """Advisory TG (squeeze/ignition/dump_hunt) off by default — log-only until edge proven."""
    return os.environ.get("HUNT_ADVISORY_TG", "0").strip().lower() in {"1", "true", "yes"}


_STATIC_BLOCK_TELEMETRY_CODES = frozenset({
    "not_anomaly",
    "scanner_continuation_wait",
    "must_pass:htf_bias_veto",
})
_BLOCK_TELEMETRY_REPEAT_MINUTES = 60


def _should_emit_blocked_telemetry(
    symbol: str,
    direction: str,
    block_code: str,
    now: datetime,
) -> bool:
    """Log static gate blocks at most once per hour (XMR not_anomaly noise)."""
    code = str(block_code or "")
    if code not in _STATIC_BLOCK_TELEMETRY_CODES and not code.startswith(
        ("family_vote_low:", "contract_")
    ):
        return True
    from hunt_core.runtime.state import current_symbol_state

    key = f"{symbol.upper()}:{direction.lower()}:{code}"
    store = current_symbol_state()
    raw = store.blocked_telemetry_log.get(key)
    if raw:
        try:
            if now - datetime.fromisoformat(str(raw)) < timedelta(
                minutes=_BLOCK_TELEMETRY_REPEAT_MINUTES
            ):
                return False
        except ValueError:
            pass
    store.blocked_telemetry_log[key] = now.isoformat()
    return True


def _maybe_emit_scanner_continuation_wait(
    *,
    symbol: str,
    direction: str,
    setup: dict[str, Any],
    lifecycle_raw: Any,
    now: datetime,
) -> None:
    """Telemetry when mid-dump has fuel but scanner withheld closed-bar confirm."""
    if direction != "short" or bool(setup.get("confirmed")):
        return
    lc = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
    fall = float(lc.get("fall_from_high_pct") or 0)
    from hunt_core.scanner.detect.setup_fields import setup_conviction_pct, setup_meets_strength

    conviction = setup_conviction_pct(setup, direction="short")
    if (
        str(lc.get("phase") or "") != "dump_active"
        or fall < 40.0
        or not setup_meets_strength(setup, direction="short", symbol=symbol, tier="confirm")
        or not _should_emit_blocked_telemetry(
            symbol, direction, "scanner_continuation_wait", now
        )
    ):
        return
    LOG.info(
        "watch_scanner_continuation_wait",
        symbol=symbol,
        fall_pct=round(fall, 1),
        conviction=round(conviction, 1),
        phase=setup.get("phase"),
    )
    append_signal_event(
        "blocked",
        symbol=symbol,
        direction=direction,
        detail="scanner_continuation_wait",
        payload={
            "block_code": "scanner_continuation_wait",
            "fall_pct": fall,
            "conviction": conviction,
            "phase": setup.get("phase"),
            "lifecycle_phase": lc.get("phase"),
        },
    )


def _confirm_delivery_suppressed(
    tracker_state: dict[str, Any],
    state: dict[str, str],
    *,
    symbol: str,
    direction: str,
    now: datetime,
) -> bool:
    """Skip confirm re-evaluation after TG shipped (avoids confirmed→blocked flicker)."""
    from hunt_core.track.tracker import (
        recent_stop_hit_cooldown,
        signal_confirm_announced,
    )

    if signal_confirm_announced(
        tracker_state, symbol=symbol, direction=direction
    ):
        return True
    if recent_stop_hit_cooldown(
        tracker_state, symbol=symbol, direction=direction, now=now
    ):
        return True
    return not unified_cooldown_ok(
        state,
        symbol=symbol,
        direction=direction,
        stage="confirm",
        now=now,
    )


def _confirm_blocked_bias_wait(
    *,
    direction: str,
    lifecycle: Any | None,
    setup: dict[str, Any] | None = None,
    symbol: str = "",
) -> bool:
    """Block confirm TG on mid-dump shorts (bias=wait in dump_active)."""
    _ = setup, symbol
    if direction != "short" or not isinstance(lifecycle, dict):
        return False
    if str(lifecycle.get("phase") or "") != "dump_active":
        return False
    return str(lifecycle.get("recommended_bias") or "") == "wait"



def hunt_auto_confirm_blocked(symbol: str) -> bool:
    """Pinned anchors never receive Module 1 hunt TG confirm (override via env)."""
    if os.environ.get("HUNT_PINNED_AUTO_CONFIRM", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return False
    from hunt_core.data.universe import PINNED_SYMBOLS

    return str(symbol or "").upper() in PINNED_SYMBOLS


__all__ = [
    "_advisory_tg_enabled",
    "_confirm_blocked_bias_wait",
    "_confirm_delivery_suppressed",
    "_maybe_emit_scanner_continuation_wait",
    "_should_emit_blocked_telemetry",
    "hunt_auto_confirm_blocked",
]

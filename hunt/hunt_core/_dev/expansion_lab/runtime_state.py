"""Persist expansion FSM + block-score history across hunt restarts."""
from __future__ import annotations

import json
import time
from typing import Any

import structlog

from hunt_core._dev.expansion_lab.config import load_expansion_config
from hunt_core._dev.expansion_lab.history import global_history
from hunt_core._dev.expansion_lab.state_machine import global_state_machine
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.paths import EXPANSION_RUNTIME_STATE_JSON

LOG = structlog.get_logger("hunt.expansion_runtime_state")

_LAST_SAVE_MONO = 0.0


def _persist_symbols() -> set[str]:
    """Symbols worth keeping history for — pinned + FSM + scan cache."""
    syms = {str(s).upper() for s in PINNED_SYMBOLS}
    syms.update(global_state_machine().snapshot().keys())
    try:
        from hunt_core.runtime.tick_state import deep_query_store, hunt_scan_store

        for store in (hunt_scan_store(), deep_query_store()):
            for row in store.all_rows():
                sym = str(row.get("symbol") or "").upper()
                if sym:
                    syms.add(sym)
    except Exception:
        pass
    return syms


def load_expansion_runtime_state() -> None:
    """Restore FSM sticky states and block-score history when expansion is enabled."""
    cfg = load_expansion_config()
    if not cfg.enabled or not EXPANSION_RUNTIME_STATE_JSON.is_file():
        return
    try:
        raw = json.loads(EXPANSION_RUNTIME_STATE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    fsm_data = raw.get("fsm")
    if isinstance(fsm_data, dict) and fsm_data:
        global_state_machine().restore(fsm_data)
        LOG.info("expansion_fsm_restored", symbols=len(fsm_data))
    if cfg.history_persist:
        hist = raw.get("history")
        if isinstance(hist, dict) and hist:
            global_history().restore(hist, max_samples=cfg.history_persist_samples)
            LOG.info("expansion_history_restored", symbols=len(hist))


def save_expansion_runtime_state() -> None:
    """Write FSM + capped history tail for watch/pinned symbols."""
    cfg = load_expansion_config()
    if not cfg.enabled:
        return
    payload: dict[str, Any] = {
        "fsm": global_state_machine().snapshot(),
    }
    if cfg.history_persist:
        syms = _persist_symbols()
        if len(syms) > cfg.history_persist_max_symbols:
            syms = set(sorted(syms)[: cfg.history_persist_max_symbols])
        payload["history"] = global_history().snapshot(
            syms,
            max_samples=cfg.history_persist_samples,
        )
    try:
        EXPANSION_RUNTIME_STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
        EXPANSION_RUNTIME_STATE_JSON.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        LOG.debug("expansion_runtime_save_failed", exc_info=True)


def maybe_save_expansion_runtime_state() -> None:
    """Throttled save — called after watch ticks (default every 5 min)."""
    global _LAST_SAVE_MONO
    cfg = load_expansion_config()
    if not cfg.enabled:
        return
    interval = max(60.0, cfg.runtime_save_interval_s)
    now = time.monotonic()
    if now - _LAST_SAVE_MONO < interval:
        return
    _LAST_SAVE_MONO = now
    save_expansion_runtime_state()


__all__ = [
    "load_expansion_runtime_state",
    "maybe_save_expansion_runtime_state",
    "save_expansion_runtime_state",
]

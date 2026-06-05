"""Radar store health assessment (isolated from runtime_ops to avoid import cycles)."""

from __future__ import annotations

import time
from typing import Any

_STALE_INGEST_SECONDS = 120.0
_STALE_SYMBOL_RATIO_ALERT = 0.35
_MIN_SYMBOLS_FOR_ALERT = 50


def assess_radar_store(
    store: object | None,
    *,
    config: object,
    now: float | None = None,
) -> dict[str, Any]:
    """JSON-safe radar health for HealthManager, telemetry, startup_report."""
    enabled = bool(getattr(config, "enabled", False))
    if store is None:
        return {"enabled": enabled, "attached": False, "status": "unavailable"}
    if not enabled:
        return {"enabled": False, "attached": True, "status": "disabled"}

    ts = float(now if now is not None else time.monotonic())
    total = int(getattr(store, "symbol_count", 0) or 0)
    tiers = store.snapshot_summary() if hasattr(store, "snapshot_summary") else {}
    stale = recent = flagged = 0
    iter_states = getattr(store, "iter_states", None)
    states = list(iter_states()) if callable(iter_states) else []
    for state in states:
        if getattr(state, "flags", ()):
            flagged += 1
        age = ts - float(getattr(state, "last_update_ts", 0.0) or 0.0)
        if age > _STALE_INGEST_SECONDS:
            stale += 1
        else:
            recent += 1

    stale_ratio = (stale / total) if total else 0.0
    status = "healthy"
    alerts: list[str] = []
    if total < _MIN_SYMBOLS_FOR_ALERT:
        status = "degraded"
        alerts.append("low_symbol_count")
    if total >= _MIN_SYMBOLS_FOR_ALERT and stale_ratio >= _STALE_SYMBOL_RATIO_ALERT:
        status = "degraded"
        alerts.append("stale_ingest_ratio_high")
    if flagged == 0 and total >= _MIN_SYMBOLS_FOR_ALERT:
        status = "degraded"
        alerts.append("zero_screener_flags")

    return {
        "enabled": True,
        "attached": True,
        "status": status,
        "alerts": alerts,
        "symbol_count": total,
        "tiers": tiers,
        "flagged_count": flagged,
        "recent_ingest_count": recent,
        "stale_ingest_count": stale,
        "stale_ingest_ratio": round(stale_ratio, 4),
        "last_tier_cycle_age_s": round(
            max(0.0, ts - float(getattr(store, "_last_tier_cycle_ts", 0.0) or 0.0)),
            2,
        ),
    }


__all__ = ["assess_radar_store"]

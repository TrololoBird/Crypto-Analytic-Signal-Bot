"""Entry-zone freshness, delivery tier, and stale hard blocks."""
from __future__ import annotations

import os
from typing import Any, Literal

DeliveryTier = Literal["armed", "triggered"]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def entry_chase_tol() -> float:
    return _env_float("HUNT_ENTRY_CHASE_TOL", 0.002)


def max_tp1_progress() -> float:
    """Max fraction of entry→TP1 move already captured before TG ships."""
    return _env_float("HUNT_MAX_TP1_PROGRESS", 0.25)


def price_in_entry_zone(
    setup: dict[str, Any],
    price: float,
    *,
    direction: str,
    tol: float | None = None,
    strict_upper: bool = False,
) -> bool:
    """True when price is inside the latched entry band (fade-at-top / in-zone dump)."""
    if price <= 0:
        return False
    ez = setup.get("entry_zone")
    try:
        zone_lo = float(ez[0])
        zone_hi = float(ez[1])
    except (TypeError, ValueError, IndexError):
        return False
    if zone_lo <= 0 or zone_hi <= 0 or zone_hi < zone_lo:
        return False
    band_tol = entry_chase_tol() if tol is None else tol
    lo_bound = zone_lo * (1.0 - band_tol)
    if direction == "short":
        hi_bound = zone_hi
    else:
        hi_bound = zone_hi if strict_upper else zone_hi * (1.0 + band_tol)
    return lo_bound <= price <= hi_bound


def delivery_freshness_block(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    chase_tol: float | None = None,
    max_progress: float | None = None,
    lifecycle: dict[str, Any] | None = None,
) -> str | None:
    """Return block code when price is too late for a new manual entry, else None."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return "delivery_bad_price"

    ez = setup.get("entry_zone")
    try:
        zone_lo = float(ez[0])
        zone_hi = float(ez[1])
    except (TypeError, ValueError, IndexError):
        return "delivery_bad_entry_geometry"

    if zone_lo <= 0 or zone_hi <= 0 or zone_hi < zone_lo:
        return "delivery_bad_entry_geometry"

    tol = entry_chase_tol() if chase_tol is None else chase_tol
    tp1 = float(setup.get("tp1") or 0)
    _ = max_progress

    lc = lifecycle if isinstance(lifecycle, dict) else row.get("lifecycle")
    lc_dict = lc if isinstance(lc, dict) else {}
    phase = str(lc_dict.get("phase") or "")
    fall = float(lc_dict.get("fall_from_high_pct") or 0)

    if direction == "short" and phase == "dump_active" and fall >= 40.0:
        if price < zone_lo or price > zone_hi * (1.0 + tol):
            return "delivery_dump_entry_stale"
    elif direction == "short":
        if tp1 > 0 and price <= tp1:
            return "delivery_past_tp1"
        if price > zone_hi:
            return "delivery_short_above_entry_zone"
        if price < zone_lo * (1.0 - tol):
            return "delivery_late_chase"
    elif direction == "long":
        if tp1 > 0 and price >= tp1:
            return "delivery_past_tp1"
        if price > zone_hi * (1.0 + tol):
            return "delivery_late_chase"
    return None


def delivery_hard_block(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
) -> str | None:
    """Hard stale blocks only — never use for ARMED/TRIGGERED routing."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return "delivery_bad_price"

    ez = setup.get("entry_zone")
    try:
        zone_lo = float(ez[0])
        zone_hi = float(ez[1])
    except (TypeError, ValueError, IndexError):
        return "delivery_bad_entry_geometry"

    if zone_lo <= 0 or zone_hi <= 0 or zone_hi < zone_lo:
        return "delivery_bad_entry_geometry"

    tp1 = float(setup.get("tp1") or 0)

    if direction == "short":
        if tp1 > 0 and price <= tp1:
            return "delivery_past_tp1"
    elif direction == "long":
        if tp1 > 0 and price >= tp1:
            return "delivery_past_tp1"

    return None


def tp1_progress_block(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    max_progress: float | None = None,
) -> str | None:
    """Optional cap for TRIGGERED tier only (not applied to ARMED)."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return None
    ez = setup.get("entry_zone")
    try:
        zone_lo = float(ez[0])
        zone_hi = float(ez[1])
    except (TypeError, ValueError, IndexError):
        return None
    tp1 = float(setup.get("tp1") or 0)
    progress_cap = max_tp1_progress() if max_progress is None else max_progress
    tol = entry_chase_tol()

    if direction == "short":
        if tp1 > 0 and zone_lo > tp1:
            total = zone_lo - tp1
            captured = zone_lo - price
            if total > 0 and captured / total > progress_cap:
                return "delivery_tp1_progress"
        if price < zone_lo * (1.0 - tol):
            return None
    elif direction == "long":
        if tp1 > 0 and tp1 > zone_hi:
            total = tp1 - zone_hi
            captured = price - zone_hi
            if total > 0 and captured / total > progress_cap:
                return "delivery_tp1_progress"
    return None


def classify_delivery_tier(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
) -> DeliveryTier | None:
    """Return tier when setup may ship; None when delivery is stale or monitor-only."""
    lc = lifecycle if isinstance(lifecycle, dict) else row.get("lifecycle")
    lc_dict = lc if isinstance(lc, dict) else {}
    setup_confirmed = bool(setup.get("confirmed") or setup.get("intrabar_confirmed"))
    from hunt_core.detect.routing import resolve_delivery_mode

    mode = resolve_delivery_mode(lc_dict, setup)

    if mode == "monitor_only" and not setup_confirmed:
        return None

    if delivery_hard_block(direction=direction, setup=setup, row=row):
        return None
    price = float(row.get("price") or 0)
    if price <= 0:
        return None

    if mode == "armed_first" and not setup_confirmed:
        return "armed"

    if price_in_entry_zone(setup, price, direction=direction):
        return "triggered"
    return "armed"


__all__ = [
    "DeliveryTier",
    "classify_delivery_tier",
    "delivery_freshness_block",
    "delivery_hard_block",
    "entry_chase_tol",
    "max_tp1_progress",
    "price_in_entry_zone",
    "tp1_progress_block",
]

"""Activation zones — catalyst / entry proximity (V2.5)."""
from __future__ import annotations

from typing import Any, Literal

from hunt_core.deep.verdict_v2._helpers import safe_float

ActivationState = Literal["idle", "near_entry", "in_entry_zone", "near_catalyst", "at_catalyst"]


def assess_activation(row: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    price = safe_float(row.get("price"))
    if price <= 0:
        return {"state": "idle", "dist_catalyst_pct": None, "dist_entry_pct": None}

    state: ActivationState = "idle"
    dist_cat: float | None = None
    dist_entry: float | None = None

    cat = summary.get("catalyst_level")
    if cat is not None:
        try:
            cp = float(cat)
            dist_cat = abs(price - cp) / price * 100.0
            if dist_cat <= 0.15:
                state = "at_catalyst"
            elif dist_cat <= 0.55 and state == "idle":
                state = "near_catalyst"
        except (TypeError, ValueError):
            pass

    lo = summary.get("entry_lo")
    hi = summary.get("entry_hi")
    try:
        el, eh = float(lo), float(hi)
        if el > 0 and eh > 0:
            if el <= price <= eh:
                state = "in_entry_zone"
            else:
                dist_entry = min(abs(price - el), abs(price - eh)) / price * 100.0
                if dist_entry <= 0.35 and state in {"idle", "near_catalyst"}:
                    state = "near_entry"
    except (TypeError, ValueError):
        pass

    return {
        "state": state,
        "dist_catalyst_pct": round(dist_cat, 3) if dist_cat is not None else None,
        "dist_entry_pct": round(dist_entry, 3) if dist_entry is not None else None,
    }

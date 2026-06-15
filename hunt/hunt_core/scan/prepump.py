"""Pre-pump scanner path (§4.2 — long bounce / squeeze-up)."""
from __future__ import annotations

from typing import Any

from hunt_core.scan._engine_impl import confirm_long, enrich_long_setup, phase_long


def evaluate_prepump(row: dict[str, Any], *, price: float, tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    long = dict(row.get("long") or {})
    long = enrich_long_setup(long, price=price, tf=tf, market=market)
    confirmed = confirm_long(long, tf=tf, market=market)
    long["confirmed"] = confirmed
    long["phase"] = phase_long(long, confirmed, symbol=str(row.get("symbol") or ""))
    return long


__all__ = ["confirm_long", "enrich_long_setup", "evaluate_prepump", "phase_long"]

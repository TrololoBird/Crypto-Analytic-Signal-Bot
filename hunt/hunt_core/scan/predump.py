"""Pre-dump scanner path (§4.1 — CONFIRM short cascade)."""
from __future__ import annotations

from typing import Any

from hunt_core.scan._engine_impl import (
    confirm_dump,
    enrich_dump_setup,
    phase_dump,
    score_dump_init,
)


def evaluate_predump(row: dict[str, Any], *, price: float, tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    dump = dict(row.get("dump") or {})
    dump = enrich_dump_setup(dump, price=price, tf=tf, market=market)
    confirmed = confirm_dump(dump, tf=tf, market=market)
    dump["confirmed"] = confirmed
    dump["phase"] = phase_dump(dump, confirmed, symbol=str(row.get("symbol") or ""))
    return dump


__all__ = ["confirm_dump", "enrich_dump_setup", "evaluate_predump", "phase_dump", "score_dump_init"]

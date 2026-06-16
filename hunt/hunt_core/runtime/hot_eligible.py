"""Hot-path symbol eligibility — only minute-sensitive symbols on kline trigger."""
from __future__ import annotations

from typing import Any

_HOT_PHASES = frozenset(
    {
        "dump_imminent",
        "dump_setup_forming",
        "exhaustion_watch",
        "dump_confirmed",
    }
)
_HOT_LC_PHASES = frozenset(
    {
        "dump_active",
        "distribution",
        "exhaustion_at_high",
        "dump_initiating",
    }
)


def filter_kline_hot_symbols(
    symbols: tuple[str, ...] | list[str],
    *,
    ignition_by_sym: dict[str, Any] | None,
    tracker_active: set[str] | frozenset[str],
    last_tick_get: Any,
    min_fuel: float = 40.0,
) -> tuple[str, ...]:
    """Return symbols worth a hot tick on 1m close (skip cold universe tail)."""
    ignited = set((ignition_by_sym or {}).keys())
    out: list[str] = []
    for raw in symbols:
        sym = str(raw).upper()
        if sym in ignited or sym in tracker_active:
            out.append(sym)
            continue
        row = last_tick_get(sym) if last_tick_get is not None else None
        if not isinstance(row, dict):
            continue
        dump = row.get("dump") or {}
        fuel = float(dump.get("dump_fuel") or 0)
        phase = str(dump.get("phase") or "")
        lc = row.get("lifecycle") or {}
        lc_phase = str(lc.get("phase") or "")
        if fuel >= min_fuel or phase in _HOT_PHASES or lc_phase in _HOT_LC_PHASES:
            out.append(sym)
    return tuple(dict.fromkeys(out))


__all__ = ["filter_kline_hot_symbols"]

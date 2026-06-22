"""Deep engine façade — orchestrates pinned + on-demand analysis."""
from __future__ import annotations

from typing import Any


def build_deep_report(row: dict[str, Any], *, symbol: str = "", **kwargs: Any) -> Any:
    from hunt_core.deep.build import build_deep_report as _build

    sym = symbol or str(row.get("symbol") or "")
    return _build(row, **kwargs)


def build_verdict_v2(row: dict[str, Any], *, symbol: str) -> dict[str, Any] | None:
    from hunt_core.deep.verdict_v2.orchestrator import build_scenario_verdict

    try:
        verdict = build_scenario_verdict(row, symbol=symbol)
    except Exception:
        return None
    return verdict.to_dict() if verdict else None


def build_pinned_verdict(row: dict[str, Any]) -> Any:
    from hunt_core.deep.pinned import build_pinned_verdict as _build

    return _build(row)


def is_pinned_symbol(symbol: str) -> bool:
    from hunt_core.deep.pinned import is_pinned_symbol as _is

    return _is(symbol)


__all__ = [
    "build_deep_report",
    "build_pinned_verdict",
    "build_verdict_v2",
    "is_pinned_symbol",
]

"""Deep engine façade — orchestrates pinned + on-demand analysis."""
from __future__ import annotations

from typing import Any


def build_deep_report(row: dict[str, Any], *, symbol: str = "", **kwargs: Any) -> Any:
    from hunt_core.deep.build import build_deep_report as _build

    symbol or str(row.get("symbol") or "")
    return _build(row, **kwargs)


def is_pinned_symbol(symbol: str) -> bool:
    from hunt_core.data.universe import is_pinned_symbol as _is

    return _is(symbol)


__all__ = [
    "build_deep_report",
    "is_pinned_symbol",
]

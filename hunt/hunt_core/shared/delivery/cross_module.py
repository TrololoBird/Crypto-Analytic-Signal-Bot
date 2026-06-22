"""Cross-module delivery conflict — Deep/Expansion vs Scanner direction (P0')."""
from __future__ import annotations

from typing import Any

_UP = frozenset({"up", "pump", "long", "bull", "pre_pump"})
_DOWN = frozenset({"down", "dump", "short", "bear", "pre_dump"})


def _deep_direction(row: dict[str, Any]) -> str | None:
    for key in ("verdict_v2", "deep_report", "deep"):
        block = row.get(key)
        if not isinstance(block, dict):
            continue
        raw = (
            block.get("decision")
            or block.get("signal_decision")
            or block.get("direction")
            or block.get("recommended_bias")
        )
        if raw is None:
            continue
        text = str(raw).lower()
        if text in {"long", "short"}:
            return text
        if text in _UP:
            return "long"
        if text in _DOWN:
            return "short"
    return None


def cross_module_delivery_block(
    row: dict[str, Any],
    *,
    direction: str,
) -> str | None:
    """Block code when another module's active direction opposes scanner delivery."""
    d = str(direction).lower()
    if d not in {"long", "short"}:
        return None

    deep_dir = _deep_direction(row)
    if deep_dir and deep_dir != d:
        return "cross_module_deep_conflict"

    exp = row.get("expansion")
    if isinstance(exp, dict):
        dom = str(exp.get("dominant") or exp.get("state") or "").lower()
        if dom in _UP and d == "short":
            return "cross_module_expansion_conflict"
        if dom in _DOWN and d == "long":
            return "cross_module_expansion_conflict"
    return None


__all__ = ["cross_module_delivery_block"]

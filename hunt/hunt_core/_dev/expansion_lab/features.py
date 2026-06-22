"""Thin bridges to existing hunt feature helpers (no recomputation)."""
from __future__ import annotations

from hunt_core._dev.expansion_lab.types import BlockContext


def structure_setup_type(ctx: BlockContext) -> str:
    """Best-effort structural setup taxonomy (sweep_reclaim / bos_retest / …).

    Reuses ``classify_structural_setup_type`` from the shared structure module, trying
    both directions and returning the first non-empty classification.
    """
    s = ctx.structure
    if not s:
        return ""
    explicit = str(s.get("setup_type") or "")
    if explicit:
        return explicit
    try:
        from hunt_core.features.structure import classify_structural_setup_type
    except Exception:
        return ""
    tf = ctx.timeframes or None
    for direction in ("long", "short"):
        try:
            label = classify_structural_setup_type(s, direction=direction, tf=tf)
        except Exception:
            label = None
        if label:
            return str(label)
    return ""


__all__ = ["structure_setup_type"]

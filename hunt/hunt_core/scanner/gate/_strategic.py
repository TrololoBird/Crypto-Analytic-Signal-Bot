"""Strategic gate mode — shadow until Phase 8 OOS promotion."""
from __future__ import annotations

import os


def strategic_gates_hard() -> bool:
    """When true, move/tradability gates block delivery; default shadow-only."""
    env = os.getenv("HUNT_STRATEGIC_GATES_HARD", "").strip().lower()
    if env in {"1", "true", "yes"}:
        return True
    if env in {"0", "false", "no"}:
        return False
    try:
        from hunt_core.domain.config import load_hunt_config

        cfg = load_hunt_config()
        gate = getattr(cfg, "gate", None)
        strategic = getattr(gate, "strategic", None) if gate is not None else None
        if strategic is not None and hasattr(strategic, "hard"):
            return bool(strategic.hard)
    except Exception:
        pass
    return False


__all__ = ["strategic_gates_hard"]

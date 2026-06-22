"""Move-significance gate — vol-normalized min move to TP1 (shadow until Phase 8)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MoveGateResult:
    ok: bool
    code: str
    message: str
    move_pct: float = 0.0
    min_required_pct: float = 0.0


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def evaluate_move_significance(
    setup: dict[str, Any],
    *,
    direction: str,
    price: float,
    shadow: bool = True,
) -> MoveGateResult:
    """Require TP1 distance >= max(0.5%, 0.8 * atr_pct) for limit-friendly signals."""
    entry = _f((setup.get("entry_zone") or [price])[0] if setup.get("entry_zone") else price, price)
    tp1 = _f(setup.get("tp1"))
    atr_pct = _f(setup.get("atr_pct") or setup.get("atr14_pct") or (setup.get("market") or {}).get("atr_pct"))
    if entry <= 0 or tp1 <= 0:
        return MoveGateResult(True, "move_skip", "geometry incomplete")
    move_pct = abs(tp1 - entry) / entry * 100.0
    min_req = max(0.5, 0.8 * atr_pct) if atr_pct > 0 else 0.8
    ok = move_pct >= min_req
    code = "move_significance_ok" if ok else "move_significance_low"
    msg = f"move {move_pct:.2f}% vs min {min_req:.2f}%"
    if shadow and not ok:
        return MoveGateResult(True, "move_shadow_warn", msg, move_pct, min_req)
    return MoveGateResult(ok, code, msg, move_pct, min_req)


__all__ = ["MoveGateResult", "evaluate_move_significance"]

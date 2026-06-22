"""Tradability / fill proxy gate — zone width vs ATR (shadow until Phase 8)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TradabilityResult:
    ok: bool
    code: str
    message: str
    zone_width_pct: float = 0.0
    fill_score: float = 0.0


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def evaluate_tradability(
    setup: dict[str, Any],
    *,
    price: float,
    shadow: bool = True,
) -> TradabilityResult:
    ez = setup.get("entry_zone") or [price, price]
    if not isinstance(ez, (list, tuple)) or len(ez) < 2:
        return TradabilityResult(True, "tradability_skip", "no entry zone")
    lo, hi = _f(ez[0]), _f(ez[1])
    mid = (lo + hi) / 2.0 if lo > 0 and hi > 0 else price
    width_pct = abs(hi - lo) / mid * 100.0 if mid > 0 else 0.0
    atr_pct = _f(setup.get("atr_pct") or (setup.get("market") or {}).get("atr_pct"))
    max_width = max(0.15, 0.35 * atr_pct) if atr_pct > 0 else 0.35
    fill_score = max(0.0, min(1.0, 1.0 - width_pct / max(max_width, 1e-6)))
    ok = width_pct <= max_width * 1.5
    code = "tradability_ok" if ok else "tradability_poor"
    msg = f"zone {width_pct:.2f}% vs max {max_width:.2f}% fill_score={fill_score:.2f}"
    if shadow and not ok:
        return TradabilityResult(True, "tradability_shadow_warn", msg, width_pct, fill_score)
    return TradabilityResult(ok, code, msg, width_pct, fill_score)


__all__ = ["TradabilityResult", "evaluate_tradability"]

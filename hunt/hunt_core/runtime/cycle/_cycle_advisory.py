"""Advisory TG helpers — squeeze cooldown only (legacy early/dump_hunt/liq removed)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from hunt_core.domain.config import COOLDOWN_MINUTES


def _cooldown_ok(
    symbol: str,
    key: str,
    state: dict[str, str],
    *,
    now: datetime,
    minutes: int = COOLDOWN_MINUTES,
) -> bool:
    raw = state.get(f"{symbol}:{key}")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    return now - last >= timedelta(minutes=minutes)


def _entry_past_tp1(
    setup: dict[str, Any] | None,
    *,
    direction: str,
    price: float,
) -> bool:
    if not setup or price <= 0:
        return False
    tp1 = setup.get("tp1")
    try:
        tp1_f = float(tp1)
    except (TypeError, ValueError):
        return False
    if direction == "short":
        return price <= tp1_f
    return price >= tp1_f


__all__ = ["_cooldown_ok", "_entry_past_tp1"]

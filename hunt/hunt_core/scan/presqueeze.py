"""Pre-squeeze volatility coil path (§4.3)."""
from __future__ import annotations

from typing import Any


def evaluate_presqueeze(tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any] | None:
    from hunt_core.runtime.tick_assembly import squeeze_watch

    return squeeze_watch(tf, market)


def format_squeeze_telegram(row: dict[str, Any]) -> str:
    from hunt_core.runtime.tick_assembly import format_squeeze_telegram as _fmt

    return _fmt(row)


__all__ = ["evaluate_presqueeze", "format_squeeze_telegram"]

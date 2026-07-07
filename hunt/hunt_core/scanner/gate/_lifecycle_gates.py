"""Lifecycle-oriented delivery gates — extracted from delivery.py (Phase 8)."""
from __future__ import annotations

from typing import Any

from hunt_core.scanner.gate._lifecycle import core_lifecycle_blockers as _core_lifecycle_blockers
from hunt_core.scanner.gate._mission import mission_delivery_block
from hunt_core.scanner.gate._rr import short_dump_delivery_too_late as _short_dump_delivery_too_late
from hunt_core.scanner.gate._types import GateResult

__all__ = [
    "GateResult",
    "collect_lifecycle_blockers",
]


def collect_lifecycle_blockers(
    setup: dict[str, Any],
    *,
    direction: str,
    lifecycle: dict[str, Any],
    row: dict[str, Any],
    symbol: str,
) -> list[GateResult]:
    """Lifecycle + phase-matrix blockers shared by report and live paths."""
    sym = symbol.upper()
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    phase = str(lc.get("phase") or "")
    blockers: list[GateResult] = []

    mission = mission_delivery_block(
        direction=direction,
        lifecycle=lc,
        setup=setup,
        symbol=sym,
        row=row,
    )
    if mission is not None:
        blockers.append(mission)

    if row.get("price_stale"):
        blockers.append(
            GateResult(
                False,
                "price_stale",
                "Цена WS/REST устарела — без TG до свежего тика",
            )
        )

    core = _core_lifecycle_blockers(setup, direction=direction, lc=lc)
    if core is not None:
        blockers.append(core)

    if direction == "short" and phase == "post_dump_bounce":
        blockers.append(
            GateResult(
                False,
                "short_blocked_bounce",
                "Шорт в post_dump_bounce запрещён — отскок после дампа",
            )
        )

    if direction == "short" and lc.get("invalidate_short"):
        blockers.append(
            GateResult(
                False,
                "lifecycle_invalidate_short",
                "Lifecycle: отскок/пробой вверх — шорт инвалидирован",
            )
        )

    if direction == "short" and not lc.get("short_entry_ok", False):
        bias = str(lc.get("recommended_bias") or "—")
        blockers.append(
            GateResult(
                False,
                "short_entry_not_ok",
                f"Lifecycle {phase or '—'} bias={bias} — вход в шорт запрещён",
            )
        )

    if (
        direction == "short"
        and bool(setup.get("impulse_confirmed") or setup.get("intrabar_confirmed"))
        and phase == "dump_active"
        and str(lc.get("recommended_bias") or "") == "wait"
    ):
        blockers.append(
            GateResult(
                False,
                "bias_wait_mid_dump",
                "Bias wait в активном дампе — monitor only",
            )
        )

    confirmed = bool(setup.get("impulse_confirmed") or setup.get("intrabar_confirmed"))
    if direction == "short" and confirmed:
        late = _short_dump_delivery_too_late(lc, setup, symbol=sym)
        if late is not None:
            blockers.append(late)

    if direction == "long" and not lc.get("long_entry_ok", True):
        bias = str(lc.get("recommended_bias") or "—")
        blockers.append(
            GateResult(
                False,
                "long_entry_not_ok",
                f"Lifecycle {phase or '—'} bias={bias} — вход в лонг запрещён",
            )
        )

    return blockers

"""Mission lock — watch TG only for imminent pre-dump / pre-pump (not mid-move).

Watch auto-scan delivers when the move is *about to* start. Mid-leg dump/pump is
monitor-only. Deep analysis for any symbol stays on the /signal query path.
"""
from __future__ import annotations

from typing import Any, Literal

from hunt_core.scanner.gate._types import GateResult

Direction = Literal["short", "long"]

from hunt_core.scanner.gate._lifecycle import (
    MID_LEG_PHASES,
    PRE_DUMP_MISSION_PHASES,
    PRE_PUMP_MISSION_PHASES,
)

# Short: fade / break *before* or at first initiation — not mid-dump.
PRE_DUMP_LIVE_LC_PHASES = PRE_DUMP_MISSION_PHASES

# Long: accumulation / bounce / coil — not parabolic leg.
PRE_PUMP_LIVE_LC_PHASES = PRE_PUMP_MISSION_PHASES

MID_DUMP_LC_PHASES = frozenset({"dump_active", "mid"})
MID_PUMP_LC_PHASES = frozenset({"impulse_initiating", "mega_leg_continuation", "mid"})

PRE_DUMP_SETUP_PHASES = frozenset(
    {
        "exhaustion_watch",
        "dump_setup_forming",
        "dump_imminent",
        "dump_initiating",
    }
)

COIL_LC_PHASES = frozenset({"coil"})

PRE_PUMP_SETUP_PHASES = frozenset(
    {
        "long_setup_forming",
        "long_imminent",
        "accumulation_watch",
        "bounce_watch",
        "breakout_watch",
    }
)

MID_LEG_LC_PHASES = MID_LEG_PHASES


def is_mid_leg_phase(phase: str) -> bool:
    """True when the move is already underway — watch must not hunt/confirm."""
    return str(phase or "") in MID_LEG_LC_PHASES


def is_watch_hunt_phase(phase: str, direction: Direction) -> bool:
    """True when lifecycle phase is a valid PRE manipulation window for direction."""
    p = str(phase or "")
    if not p or is_mid_leg_phase(p):
        return False
    if direction == "short":
        return p in PRE_DUMP_LIVE_LC_PHASES
    return p in PRE_PUMP_LIVE_LC_PHASES


def hunt_skip_reason(phase: str, direction: Direction) -> str | None:
    """Machine reason when watch confirm/delivery candidacy should be skipped."""
    p = str(phase or "")
    if not p:
        return None
    if is_mid_leg_phase(p):
        return "mid_leg"
    if not is_watch_hunt_phase(p, direction):
        return "non_pre_phase"
    return None


def _fall_pct(lc: dict[str, Any]) -> float:
    return float(lc.get("fall_from_high_pct") or 0)


def _leg_gain_pct(lc: dict[str, Any]) -> float:
    for key in ("leg_gain_pct", "rally_from_24h_low_pct", "rise_from_low_pct"):
        v = lc.get(key)
        if v is not None:
            return float(v)
    return 0.0


def mission_delivery_block(
    *,
    direction: str,
    lifecycle: dict[str, Any] | None,
    setup: dict[str, Any] | None = None,
    symbol: str = "",
) -> GateResult | None:
    """Hard block watch-path TG when the move already started or wrong archetype."""
    from hunt_core.scanner.gate._rr import (
        short_dump_first_break_max_fall_pct,
        short_dump_start_max_fall_pct,
    )

    lc = lifecycle if isinstance(lifecycle, dict) else {}
    phase = str(lc.get("phase") or "")
    d = direction.lower().strip()
    sym = symbol.upper()
    fall = _fall_pct(lc)
    break_max = short_dump_first_break_max_fall_pct(sym)
    start_max = short_dump_start_max_fall_pct(sym)

    if d == "short":
        if phase in MID_DUMP_LC_PHASES:
            return GateResult(
                False,
                "mission_mid_dump",
                f"Дамп уже идёт ({phase}, −{fall:.1f}% от хая) — monitor only, без TG",
            )
        if phase in COIL_LC_PHASES:
            return None
        if phase not in PRE_DUMP_LIVE_LC_PHASES:
            return GateResult(
                False,
                "mission_not_pre_dump",
                f"Фаза {phase or '—'} вне pre-dump окна (exhaustion/distribution/initiating)",
            )
        if phase != "exhaustion_at_high" and fall > break_max:
            return GateResult(
                False,
                "mission_dump_already_falling",
                f"Уже −{fall:.1f}% от хая (> {break_max:.0f}%) — дамп начался, не «вот-вот»",
            )
        if isinstance(setup, dict):
            sp = str(setup.get("phase") or "")
            if sp == "dump_confirmed" and fall > start_max:
                return GateResult(
                    False,
                    "mission_dump_confirmed_late",
                    f"dump_confirmed при −{fall:.1f}% — поздний вход, не pre-dump",
                )
            if setup.get("watch_only") and not setup.get("intrabar_confirmed"):
                return GateResult(
                    False,
                    "watch_only",
                    "Monitor-only (continuation) — не для delivery",
                )
        return None

    if d == "long":
        if phase in MID_PUMP_LC_PHASES:
            leg = _leg_gain_pct(lc)
            return GateResult(
                False,
                "mission_mid_pump",
                f"Памп уже идёт ({phase}, leg +{leg:.1f}%) — monitor only, без TG",
            )
        if phase in COIL_LC_PHASES:
            return None
        if phase not in PRE_PUMP_LIVE_LC_PHASES:
            return GateResult(
                False,
                "mission_not_pre_pump",
                f"Фаза {phase or '—'} вне pre-pump окна (accumulation/bounce/coil)",
            )
        leg = _leg_gain_pct(lc)
        if leg >= 10.0 and phase not in {"post_dump_bounce", "recovery"}:
            return GateResult(
                False,
                "mission_pump_already_rising",
                f"Leg уже +{leg:.1f}% — памп начался, не «вот-вот»",
            )
        if isinstance(setup, dict) and setup.get("watch_only") and not setup.get(
            "intrabar_confirmed"
        ):
            return GateResult(
                False,
                "watch_only",
                "Monitor-only — не для delivery",
            )
        return None

    return None


def mission_live_phase_ok(
    direction: str,
    lifecycle: dict[str, Any] | None,
    *,
    setup: dict[str, Any] | None = None,
    symbol: str = "",
) -> bool:
    return (
        mission_delivery_block(
            direction=direction,
            lifecycle=lifecycle,
            setup=setup,
            symbol=symbol,
        )
        is None
    )


__all__ = [
    "MID_DUMP_LC_PHASES",
    "MID_LEG_LC_PHASES",
    "MID_PUMP_LC_PHASES",
    "PRE_DUMP_LIVE_LC_PHASES",
    "PRE_DUMP_SETUP_PHASES",
    "PRE_PUMP_LIVE_LC_PHASES",
    "PRE_PUMP_SETUP_PHASES",
    "hunt_skip_reason",
    "is_mid_leg_phase",
    "is_watch_hunt_phase",
    "mission_delivery_block",
    "mission_live_phase_ok",
]

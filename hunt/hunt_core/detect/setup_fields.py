"""Setup-field accessors on the fusion engine output (single source, no EV catalog).

The fusion engine writes ``p_win`` (calibrated confidence), ``magnitude`` and
``confirmed`` onto each setup. These accessors read those directly — there is no
expected-value model, conviction fuel, or strength tier behind them anymore; delivery
strength *is* the ``confirmed`` flag.
"""
from __future__ import annotations

from typing import Any

# No legacy EV blockers under the fusion engine.
EV_PRIMARY_LEGACY_BLOCKERS: frozenset[str] = frozenset()


def setup_p_win(setup: dict[str, Any]) -> float | None:
    try:
        v = setup.get("p_win")
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def setup_conviction_pct(setup: dict[str, Any], *, direction: str = "") -> float:
    p = setup_p_win(setup)
    return p * 100.0 if p is not None else 0.0


def setup_meets_strength(
    setup: dict[str, Any],
    *,
    direction: str = "",
    symbol: str = "",
    tier: str = "confirm",
    row: dict[str, Any] | None = None,
) -> bool:
    """Delivery strength is the fusion gate decision."""
    return bool(setup.get("confirmed") or setup.get("intrabar_confirmed"))


def pwin_gate_enabled() -> bool:
    return True


def legacy_fuel_delivery_enabled() -> bool:
    return False


def delivery_ev_floors(symbol: str = "") -> tuple[float, float]:
    """(min_ev, min_p_win) floors from calibration."""
    try:
        from hunt_core.params.store import delivery_thresholds

        dl = delivery_thresholds(symbol)
        return float(dl.get("min_ev", 0.0)), float(dl.get("min_p_win", 0.42))
    except Exception:
        return 0.0, 0.42


def resolve_delivery_ev(setup: dict[str, Any], direction: str = "") -> dict[str, Any]:
    return {"ev": None, "p_win": setup_p_win(setup), "source": "fusion"}


def ev_primary_delivery_qualified(
    setup: dict[str, Any], direction: str = "", symbol: str = ""
) -> bool:
    return bool(setup.get("confirmed") or setup.get("intrabar_confirmed"))


__all__ = [
    "EV_PRIMARY_LEGACY_BLOCKERS",
    "delivery_ev_floors",
    "ev_primary_delivery_qualified",
    "legacy_fuel_delivery_enabled",
    "pwin_gate_enabled",
    "resolve_delivery_ev",
    "setup_conviction_pct",
    "setup_meets_strength",
    "setup_p_win",
]

"""H-A sniper product gate — short fade in dump_active only (Gate G2)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SniperConfig:
    """Live TG delivery restricted to data-validated edge slice."""

    enabled: bool = True
    live_phases: frozenset[str] = frozenset({"dump_active"})
    top_ls_max: float = 2.0
    require_top_ls: bool = True
    chase_tol: float = 0.002

    @classmethod
    def from_env(cls) -> SniperConfig:
        wide = os.environ.get("HUNT_WIDE_MODE", "1") not in {"0", "false", "False"}
        default_sniper = "0" if wide else "1"
        off = os.environ.get("HUNT_SNIPER_MODE", default_sniper) in {"0", "false", "False"}
        require_ls = os.environ.get("HUNT_SNIPER_REQUIRE_TOP_LS", "1") not in {"0", "false", "False"}
        return cls(
            enabled=not off,
            top_ls_max=float(os.environ.get("HUNT_SNIPER_TOP_LS_MAX", "2.0")),
            require_top_ls=require_ls,
            chase_tol=float(os.environ.get("HUNT_SNIPER_CHASE_TOL", "0.002")),
        )


def sniper_block_reason(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    config: SniperConfig | None = None,
) -> str | None:
    """Return a machine block code if sniper mode vetoes TG delivery, else None."""
    cfg = config or SniperConfig.from_env()
    if not cfg.enabled:
        return None
    if direction != "short":
        return "sniper_long_shadow"
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    phase = str(lc.get("phase") or "")
    if phase not in cfg.live_phases:
        return f"sniper_phase:{phase or 'unknown'}"
    if lc.get("short_entry_ok") is not True:
        return "sniper_short_entry_not_ok"
    ez = setup.get("entry_zone")
    try:
        zone_lo = float(ez[0])
        px = float(row["price"])
    except (TypeError, ValueError, IndexError, KeyError):
        return "sniper_bad_entry_geometry"
    if px < zone_lo * (1.0 - cfg.chase_tol):
        return "sniper_late_chase"
    top_ls = (row.get("market") or {}).get("top_ls_1h")
    if top_ls is None:
        if cfg.require_top_ls:
            return "sniper_top_ls_missing"
        return None
    try:
        top_ls_f = float(top_ls)
    except (TypeError, ValueError):
        return "sniper_top_ls_bad"
    if top_ls_f >= cfg.top_ls_max:
        return "sniper_top_ls_high"
    return None

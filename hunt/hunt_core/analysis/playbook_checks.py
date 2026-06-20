"""Shared playbook checklist — single source for fusion rank and delivery gates."""
from __future__ import annotations

from typing import Any

# Required checks per archetype (named standards, not fuel weights).
PLAYBOOK_REQUIRED: dict[str, frozenset[str]] = {
    "predump_short": frozenset(
        {
            "distribution_phase",
            "pos_near_high",
            "oi_distribution",
            "bear_cvd_div",
            "sweep_reclaim",
            "anti_squeeze",
        }
    ),
    "coil_long": frozenset(
        {
            "coil_phase",
            "vp_accumulation",
            "va_contraction",
            "bid_absorption",
            "bull_cvd_div",
            "vah_break_5m",
            "vol_above_median_5m",
        }
    ),
    "ignition_long": frozenset(
        {
            "neg_funding",
            "short_liq_above",
            "squeeze_regime",
            "cvd_absorption",
            "obi_bid",
        }
    ),
}

PLAYBOOK_N_OF_M: dict[str, tuple[int, int]] = {
    "predump_short": (4, 6),
    "coil_long": (5, 7),
    "ignition_long": (5, 5),
}

# Display-only smart-money context (not in N-of-M required sets).
SMART_MONEY_DISPLAY_CHECKS = frozenset({"vol_oi_sane", "flow_aligned"})


def playbook_pass_count(
    archetype: str,
    checks: dict[str, bool],
) -> tuple[int, int]:
    """Return (pass_count, required_n) for archetype checklist."""
    if archetype == "none":
        return 0, 0
    keys = PLAYBOOK_REQUIRED.get(str(archetype), frozenset())
    if not keys:
        return 0, 0
    passed = sum(1 for k in keys if checks.get(k))
    _, required = PLAYBOOK_N_OF_M.get(str(archetype), (len(keys), len(keys)))
    return passed, required


def playbook_pass_ratio(archetype: str, checks: dict[str, bool]) -> float:
    """0–100 rank score from pass ratio (canonical primary_score)."""
    passed, required = playbook_pass_count(archetype, checks)
    if required <= 0:
        return 0.0
    return round(100.0 * passed / required, 1)


def playbook_passes(archetype: str, checks: dict[str, bool]) -> bool:
    passed, required = playbook_pass_count(archetype, checks)
    if required <= 0:
        return False
    return passed >= required


def best_archetype_by_ratio(checks: dict[str, bool]) -> tuple[str, float, int, int]:
    """Pick archetype with highest pass ratio."""
    best_arch = "none"
    best_ratio = 0.0
    best_pc = 0
    best_req = 0
    for arch in ("predump_short", "coil_long", "ignition_long"):
        pc, req = playbook_pass_count(arch, checks)
        ratio = (100.0 * pc / req) if req > 0 else 0.0
        if ratio > best_ratio or (ratio == best_ratio and pc > best_pc):
            best_arch = arch
            best_ratio = ratio
            best_pc = pc
            best_req = req
    if best_pc <= 0:
        return "none", 0.0, 0, 0
    return best_arch, round(best_ratio, 1), best_pc, best_req


__all__ = [
    "PLAYBOOK_N_OF_M",
    "PLAYBOOK_REQUIRED",
    "SMART_MONEY_DISPLAY_CHECKS",
    "best_archetype_by_ratio",
    "playbook_pass_count",
    "playbook_pass_ratio",
    "playbook_passes",
]

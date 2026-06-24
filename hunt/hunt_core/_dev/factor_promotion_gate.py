"""Factor promotion gate — quarantine → production (Phase 8 OOS hard gate)."""
from __future__ import annotations

import math

from hunt_core.scanner.detect.factor_registry_loader import (
    factor_status,
    load_factor_registry,
    quarantine_factors,
)

# Minimum OOS precision lift vs baseline to promote one factor (multiple-comparison bump).
_DEFAULT_MIN_OOS_DELTA = 0.03
_MC_BONUS_PER_QUARANTINE = 0.005


def promotion_min_delta(*, quarantine_count: int | None = None) -> float:
    n = quarantine_count if quarantine_count is not None else len(quarantine_factors())
    return _DEFAULT_MIN_OOS_DELTA + _MC_BONUS_PER_QUARANTINE * max(0, n - 1)


def promotion_allowed(
    name: str,
    *,
    oos_precision_delta: float = 0.0,
    min_delta: float | None = None,
    delivered_n: int = 0,
) -> bool:
    """Promote quarantine factor only on positive OOS lift with enough outcomes."""
    if factor_status(name) == "production":
        return True
    if factor_status(name) != "quarantine":
        return False
    floor = min_delta if min_delta is not None else promotion_min_delta()
    need_n = min_outcomes_for_power()
    if delivered_n < need_n:
        return False
    return oos_precision_delta >= floor


def min_outcomes_for_power(
    *,
    baseline_wr: float = 0.5,
    mde: float = 0.15,
    alpha: float = 0.05,
    power: float = 0.8,
) -> int:
    """Rough binomial sample size for promotion ramp (replaces arbitrary n≥30)."""
    z_alpha = 1.96 if alpha <= 0.05 else 1.645
    z_beta = 0.84 if power >= 0.8 else 0.52
    p1 = baseline_wr
    p2 = min(0.99, baseline_wr + mde)
    num = (z_alpha * math.sqrt(2 * p1 * (1 - p1)) + z_beta * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    den = (p2 - p1) ** 2
    return max(30, int(math.ceil(num / den))) if den > 0 else 30


def main() -> int:
    reg = load_factor_registry()
    factors = reg.get("factors") or {}
    prod = [k for k, v in factors.items() if v.get("status") == "production"]
    quarantine = [k for k, v in factors.items() if v.get("status") == "quarantine"]
    print(
        f"factor_promotion_gate ok | production={len(prod)} quarantine={len(quarantine)} "
        f"min_oos_delta={promotion_min_delta():.3f} need_n={min_outcomes_for_power()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

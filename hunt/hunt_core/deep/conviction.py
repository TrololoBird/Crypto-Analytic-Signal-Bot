"""Deep conviction scoring via shared primitives."""
from __future__ import annotations

from hunt_core.analysis.primitives import conviction_from_z


def scenario_conviction(*z_scores: float | None) -> float:
    vals = [conviction_from_z(z) for z in z_scores if z is not None]
    if not vals:
        return 0.0
    return min(100.0, sum(vals) / len(vals))


__all__ = ["scenario_conviction"]

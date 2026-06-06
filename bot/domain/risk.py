"""Unified risk/reward trade-plan parameters for strategy outputs."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskParams:
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    direction: str

    def rr1(self) -> float:
        risk = abs(self.entry - self.stop)
        if risk <= 0.0:
            return 0.0
        return abs(self.tp1 - self.entry) / risk

    def rr2(self) -> float:
        risk = abs(self.entry - self.stop)
        if risk <= 0.0:
            return 0.0
        return abs(self.tp2 - self.entry) / risk

    def rr3(self) -> float:
        risk = abs(self.entry - self.stop)
        if risk <= 0.0:
            return 0.0
        return abs(self.tp3 - self.entry) / risk

    def validate(self, *, min_rr: float = 1.9) -> list[str]:
        issues: list[str] = []
        direction = str(self.direction or "").lower()
        values = (self.entry, self.stop, self.tp1, self.tp2, self.tp3)
        if not all(math.isfinite(value) and value > 0.0 for value in values):
            issues.append("non_positive_or_non_finite_levels")
        if direction == "long":
            if self.stop >= self.entry:
                issues.append("long_stop_not_below_entry")
            if not (self.entry < self.tp1 <= self.tp2 <= self.tp3):
                issues.append("long_targets_not_ordered")
        elif direction == "short":
            if self.stop <= self.entry:
                issues.append("short_stop_not_above_entry")
            if not (self.entry > self.tp1 >= self.tp2 >= self.tp3):
                issues.append("short_targets_not_ordered")
        else:
            issues.append("invalid_direction")
        if self.rr1() + 1e-9 < float(min_rr):
            issues.append("tp1_rr_below_minimum")
        return issues

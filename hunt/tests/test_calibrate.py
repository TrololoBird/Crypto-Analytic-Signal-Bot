"""Unit tests for detect/calibrate.py — degenerate inputs and cold-start."""
from __future__ import annotations

import math
import unittest

import numpy as np
import polars as pl

from hunt_core.detect import calibrate as C
from hunt_core.detect import fusion as Fz
from hunt_core.detect.factors import DIRECTIONAL, FactorScore


class TestRobustZ(unittest.TestCase):
    def test_cold_start_abstains(self) -> None:
        s = pl.Series([1.0, 2.0, 3.0])
        self.assertIsNone(C.robust_z(s, min_n=30))

    def test_flat_window_returns_zero(self) -> None:
        s = pl.Series([5.0] * 40)
        self.assertEqual(C.robust_z(s), 0.0)

    def test_nan_inf_stripped(self) -> None:
        vals = [float("nan")] * 10 + [1.0] * 35 + [99.0]
        s = pl.Series(vals)
        z = C.robust_z(s)
        self.assertIsNotNone(z)
        assert z is not None
        self.assertTrue(math.isfinite(z))
        self.assertGreater(z, 3.0)

    def test_quantile_gate_cold_start(self) -> None:
        s = pl.Series([1.0, 2.0, 3.0])
        self.assertIsNone(C.quantile_gate(s, 0.9, min_n=30))


class TestFusion(unittest.TestCase):
    def test_median_not_inflated_by_correlated_factors(self) -> None:
        factors = [
            FactorScore("book", DIRECTIONAL, 3.0, True),
            FactorScore("flow", DIRECTIONAL, 2.8, True),
            FactorScore("structure", DIRECTIONAL, -0.2, True),
        ]
        fused = Fz.fuse(factors)
        # Stouffer would give ~(3+2.8-0.2)/sqrt(3) ≈ 3.1; median is ~2.8 magnitude side.
        self.assertLess(abs(fused.z_dir), 3.05)
        self.assertGreater(fused.fusion_score, 0.0)

    def test_vol_adjusted_magnitude_flat_tape(self) -> None:
        raw = 2.0
        low_atr = Fz.vol_adjusted_magnitude(raw, 0.05)
        high_atr = Fz.vol_adjusted_magnitude(raw, 2.0)
        self.assertGreater(low_atr, high_atr)

    def test_global_gate_floor(self) -> None:
        fused = Fz.fuse([
            FactorScore("book", DIRECTIONAL, 0.50, True),
            FactorScore("flow", DIRECTIONAL, 0.55, True),
        ])
        hist = pl.Series([0.1] * 40, dtype=pl.Float64)
        decision = Fz.gate(fused, hist, q=0.5, atr_pct=1.0)
        self.assertFalse(decision.gate_open)
        self.assertEqual(decision.reason, "below_calibrated_gate")
        self.assertGreaterEqual(decision.threshold or 0, 0.55)

    def test_mad_epsilon_caps_flat_window(self) -> None:
        s = pl.Series([1.0] * 39 + [1.0001])
        z = C.robust_z(s)
        self.assertIsNotNone(z)
        assert z is not None
        self.assertLess(abs(z), 12.1)


if __name__ == "__main__":
    unittest.main()

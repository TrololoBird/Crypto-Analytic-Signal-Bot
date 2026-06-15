#!/usr/bin/env python3
"""Backward-compat — canonical: ``python -m hunt_research.calibrate.verify_diff``."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunt"))

if __name__ == "__main__":
    runpy.run_module("hunt_research.calibrate.verify_diff", run_name="__main__")

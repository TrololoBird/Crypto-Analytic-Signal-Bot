#!/usr/bin/env python3
"""Backward-compat — research batch replay (legacy independent_batch)."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_legacy = Path(__file__).resolve().parents[1] / "hunt" / "_legacy" / "scripts"
if _legacy.is_dir():
    runpy.run_path(str(_legacy / "independent_batch.py"), run_name="__main__")
else:
    print("independent_batch archived — use hunt_research.calibrate.jsonl_replay", file=sys.stderr)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunt"))
    runpy.run_module("hunt_research.calibrate.jsonl_replay", run_name="__main__")

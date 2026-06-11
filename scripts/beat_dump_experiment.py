#!/usr/bin/env python3
"""Repo-root shim → hunt/scripts/beat_dump_experiment.py"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(_ROOT / "hunt"), str(_ROOT)]
runpy.run_path(str(_ROOT / "hunt" / "scripts" / "beat_dump_experiment.py"), run_name="__main__")

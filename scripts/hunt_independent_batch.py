#!/usr/bin/env python3
"""Backward-compat entry — canonical: hunt-watch/scripts/independent_batch.py"""

from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "hunt-watch" / "scripts" / "independent_batch.py"),
        run_name="__main__",
    )

#!/usr/bin/env python3
"""Backward-compat entry — canonical: hunt/scripts/scanner.py"""

from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "hunt" / "scripts" / "scanner.py"),
        run_name="__main__",
    )

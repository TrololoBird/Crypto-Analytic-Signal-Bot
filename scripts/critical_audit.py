#!/usr/bin/env python3
"""Shim → hunt/scripts/critical_audit.py"""
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent.parent / "hunt" / "scripts" / "critical_audit.py"))

#!/usr/bin/env python3
"""Backward-compat — one-shot scanner tick (``watch --once``)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunt"))

from hunt_core.__main__ import main

if __name__ == "__main__":
    argv = ["watch", "--once", *sys.argv[1:]]
    raise SystemExit(main(argv))

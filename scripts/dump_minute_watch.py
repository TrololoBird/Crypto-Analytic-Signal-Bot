#!/usr/bin/env python3
"""Backward-compat entry — canonical: ``python -m hunt_core watch``."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunt"))

from hunt_core.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(["watch", *sys.argv[1:]]))

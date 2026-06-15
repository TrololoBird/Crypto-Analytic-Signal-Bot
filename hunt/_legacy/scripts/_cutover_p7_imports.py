#!/usr/bin/env python3
"""Rewire detect.engine/lifecycle shims → scan/regime (P7 cutover)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "hunt_core"

REPLACEMENTS = [
    ("from hunt_core.detect.engine import", "from hunt_core.scan._engine_impl import"),
    ("from hunt_core.detect import engine as _engine", "from hunt_core.scan import _engine_impl as _engine"),
    ("from hunt_core.detect.lifecycle import", "from hunt_core.regime.leg_fsm import"),
    ("from hunt_core.setups.detectors import", "from hunt_core.scan.detectors import"),
    ("from hunt_core.detect.setup_candidates import", "from hunt_core.track.candidates import"),
]

SKIP = {"_cutover_p7_imports.py", "_engine_impl.py"}


def main() -> None:
    changed = 0
    for path in ROOT.rglob("*.py"):
        if path.name in SKIP:
            continue
        text = path.read_text(encoding="utf-8")
        new = text
        for old, new_s in REPLACEMENTS:
            new = new.replace(old, new_s)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed += 1
            print(f"updated {path.relative_to(ROOT.parent)}")
    print(f"done: {changed} files")


if __name__ == "__main__":
    main()

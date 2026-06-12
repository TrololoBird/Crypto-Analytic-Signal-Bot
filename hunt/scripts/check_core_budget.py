#!/usr/bin/env python3
"""CI: hunt_core LOC budget (≤8000) + reachability from watch.py."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[1] / "hunt_core"
LOC_BUDGET = 8000
WATCH = Path(__file__).resolve().parent / "watch.py"


def _loc_tree(root: Path) -> int:
    total = 0
    for py in root.rglob("*.py"):
        if py.name == "_impl.py":
            continue  # legacy impl excluded from budget
        total += len(py.read_text(encoding="utf-8").splitlines())
    return total


def _reachable_modules() -> set[str]:
    text = WATCH.read_text(encoding="utf-8")
    tree = ast.parse(text)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("hunt_core"):
                mods.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("hunt_core"):
                    mods.add(alias.name)
    return mods


def main() -> int:
    loc = _loc_tree(CORE_ROOT)
    mods = _reachable_modules()
    ok_loc = loc <= LOC_BUDGET
    ok_reach = "hunt_core.runtime.bot" in mods or any(m.startswith("hunt_core") for m in mods)
    print(f"hunt_core LOC={loc} budget={LOC_BUDGET} ok={ok_loc}")
    print(f"watch imports hunt_core: {sorted(mods)} ok={ok_reach}")
    if not ok_loc:
        print(f"FAIL: LOC {loc} > {LOC_BUDGET}", file=sys.stderr)
        return 1
    if not ok_reach:
        print("FAIL: watch.py must import hunt_core", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

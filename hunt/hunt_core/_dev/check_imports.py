"""Import hygiene gate — §P.3 dependency direction."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]

FORBIDDEN = frozenset({"engine", "bot", "hunt_watch", "hunt_research", "intel"})
# Layers that must not import orchestration/delivery
STRICT_LOWER = frozenset({"market", "data"})
FORBIDDEN_IN_STRICT = frozenset({"scan", "analysis", "gate", "deliver", "runtime", "detect", "track", "regime", "confluence", "levels"})


def _violations() -> list[str]:
    out: list[str] = []
    for py in CORE.rglob("*.py"):
        if "_dev" in py.parts or py.name == "_impl.py":
            continue
        rel = py.relative_to(CORE)
        top = rel.parts[0] if rel.parts else ""
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod: str | None = None
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in FORBIDDEN:
                        out.append(f"{rel}: forbidden {alias.name}")
                continue
            if not mod or not mod.startswith("hunt_core."):
                continue
            root = mod.split(".", 1)[0]
            if root in FORBIDDEN:
                out.append(f"{rel}: forbidden {mod}")
            sub = mod.split(".", 1)[1] if "." in mod else ""
            sub_top = sub.split(".", 1)[0] if sub else ""
            if top in STRICT_LOWER and sub_top in FORBIDDEN_IN_STRICT:
                if top == "data" and rel.name == "scanner.py":
                    continue
                out.append(f"{rel}: upward import {mod}")
    return out


def main() -> int:
    v = _violations()
    print(f"import violations: {len(v)} ok={len(v) == 0}")
    for line in v[:30]:
        print(f"  {line}", file=sys.stderr)
    return 1 if v else 0


if __name__ == "__main__":
    raise SystemExit(main())

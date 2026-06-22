"""Import hygiene — bot/engine forbidden + module boundary lint."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]

FORBIDDEN = frozenset({"engine", "bot", "hunt_watch", "hunt_research", "intel"})
STALE_MODULES = frozenset({"hunt_core.coercion", "hunt_core.gate", "hunt_core.detect", "hunt_core.setups"})
STRICT_LOWER = frozenset({"market", "data", "shared"})
FORBIDDEN_IN_STRICT = frozenset(
    {
        "scan",
        "analysis",
        "gate",
        "deliver",
        "runtime",
        "detect",
        "track",
        "regime",
        "confluence",
        "levels",
        "scanner",
        "deep",
    }
)
UPWARD_IMPORT_ALLOWLIST: dict[str, frozenset[str]] = {
    "data/lake.py": frozenset({"runtime"}),
}

# scanner/playbook is the only scanner subtree allowed to import analysis (consolidation shim).
SCANNER_ANALYSIS_SHIM_PREFIX = "scanner/playbook/"


def _violations() -> list[str]:
    out: list[str] = []
    for py in CORE.rglob("*.py"):
        if "_dev" in py.parts or py.name == "_impl.py":
            continue
        rel = py.relative_to(CORE)
        rel_s = str(rel)
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
            parts = mod.split(".")
            sub_root = parts[1] if len(parts) > 1 else parts[0]
            if sub_root in FORBIDDEN or (parts[0] == "hunt_core" and sub_root in FORBIDDEN):
                out.append(f"{rel}: forbidden {mod}")
            sub = mod.split(".", 1)[1] if "." in mod else ""
            sub_top = sub.split(".", 1)[0] if sub else ""
            if top in STRICT_LOWER and sub_top in FORBIDDEN_IN_STRICT:
                if top == "data" and rel.name == "scanner.py":
                    continue
                if rel_s in UPWARD_IMPORT_ALLOWLIST and sub_top in UPWARD_IMPORT_ALLOWLIST[rel_s]:
                    continue
                out.append(f"{rel}: upward import {mod}")
            if top == "shared" and sub_top in {"scanner", "deep", "analysis", "gate", "detect"}:
                if rel.parts[:2] == ("shared", "mathlib"):
                    continue
                out.append(f"{rel}: shared imports decision module {mod}")
            if top == "deep" and sub_top == "scanner":
                out.append(f"{rel}: deep→scanner {mod}")
            if top == "scanner" and sub_top == "analysis" and not rel_s.startswith(SCANNER_ANALYSIS_SHIM_PREFIX):
                out.append(f"{rel}: scanner→analysis {mod} (use scanner.playbook shim)")
            if top == "analysis" and sub_top == "scanner":
                out.append(f"{rel}: analysis→scanner {mod}")
            if mod in STALE_MODULES:
                out.append(f"{rel}: stale module {mod}")
    return out


def main() -> int:
    v = _violations()
    print(f"import violations: {len(v)} ok={len(v) == 0}")
    for line in v[:40]:
        print(f"  {line}", file=sys.stderr)
    return 1 if v else 0


if __name__ == "__main__":
    raise SystemExit(main())

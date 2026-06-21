"""CI: hunt_core LOC budget + import hygiene."""
from __future__ import annotations



import ast
import sys
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[1]
ENTRY = CORE_ROOT / "__main__.py"
# Hot-path only — _dev/ and legacy gate/ (_dev/check_logic compat; fusion runtime bypasses gate).
LOC_BUDGET = 58_000  # pre-launch expansion: gate splits, EV shadow, cycle tick (~57.8k, 2026-06-18)
LOC_SKIP_DIRS = frozenset({"_dev", "gate"})

FORBIDDEN_IMPORTS: frozenset[str] = frozenset(
    {
        "engine",
        "bot",
        "hunt_watch",
        "hunt_research",
        "intel",
    }
)


def _loc_tree(root: Path) -> int:
    total = 0
    for py in root.rglob("*.py"):
        if py.name == "_impl.py":
            continue
        if LOC_SKIP_DIRS & set(py.relative_to(root).parts):
            continue
        total += len(py.read_text(encoding="utf-8").splitlines())
    return total


def _forbidden_imports_in_core(root: Path) -> list[tuple[str, str]]:
    violations: list[tuple[str, str]] = []
    for py in root.rglob("*.py"):
        if py.name == "_impl.py":
            continue
        rel = py.relative_to(root.parent)
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod: str | None = None
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    if top in FORBIDDEN_IMPORTS:
                        violations.append((str(rel), alias.name))
                continue
            if mod:
                top = mod.split(".", 1)[0]
                if top in FORBIDDEN_IMPORTS:
                    violations.append((str(rel), mod))
    return violations


def _entry_reachable() -> bool:
    if not ENTRY.is_file():
        return False
    text = ENTRY.read_text(encoding="utf-8")
    return "hunt_core.runtime._impl" in text or "runtime._impl" in text


def main() -> int:
    loc = _loc_tree(CORE_ROOT)
    violations = _forbidden_imports_in_core(CORE_ROOT)
    ok_loc = loc <= LOC_BUDGET
    ok_entry = _entry_reachable()
    ok_imports = len(violations) == 0
    print(f"hunt_core LOC={loc} budget={LOC_BUDGET} ok={ok_loc}")
    print(f"__main__.py reaches runtime ok={ok_entry}")
    print(f"forbidden imports: {len(violations)} ok={ok_imports}")
    if violations:
        for path, mod in violations[:20]:
            print(f"  {path}: {mod}", file=sys.stderr)
    if not ok_loc:
        print(f"FAIL: LOC {loc} > {LOC_BUDGET}", file=sys.stderr)
        return 1
    if not ok_entry:
        print("FAIL: hunt_core/__main__.py must reach runtime._impl", file=sys.stderr)
        return 1
    if not ok_imports:
        print("FAIL: hunt_core must not import engine/bot/hunt_watch", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

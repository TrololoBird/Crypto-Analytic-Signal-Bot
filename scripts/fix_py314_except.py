#!/usr/bin/env python3
"""Parenthesize multi-type except clauses (required on Python 3.14+).

Ruff format <0.16 may rewrite ``except (A, B):`` back to legacy syntax; run this
after ``ruff format`` when needed, or from CI before compile/tests.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXCEPT_RE = re.compile(r"^(\s*)except\s+(.+?)\s*:\s*(#.*)?$")


def fix_line(line: str) -> str:
    match = EXCEPT_RE.match(line)
    if not match:
        return line
    indent, types, comment = match.group(1), match.group(2).strip(), match.group(3) or ""
    if " as " in types or types.startswith("(") or "," not in types:
        return line
    return f"{indent}except ({types}):{comment}"


def fix_tree(root: Path) -> int:
    changed = 0
    for py in sorted(root.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        text = py.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        new_lines: list[str] = []
        file_changed = False
        for line in lines:
            if line.lstrip().startswith("except "):
                fixed = fix_line(line.rstrip("\n"))
                if fixed != line.rstrip("\n"):
                    line = fixed + ("\n" if line.endswith("\n") else "")
                    file_changed = True
            new_lines.append(line)
        if file_changed:
            py.write_text("".join(new_lines), encoding="utf-8")
            changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[REPO_ROOT / "bot"],
        help="Roots to scan (default: bot/)",
    )
    args = parser.parse_args()
    total = 0
    for path in args.paths:
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            return 1
        total += fix_tree(path)
    print(f"[OK] parenthesized except clauses in {total} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

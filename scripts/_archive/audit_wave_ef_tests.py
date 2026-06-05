#!/usr/bin/env python3
"""PROMPT 2.3: audit test_wave_e* and test_wave_f* — append to _audit_scratch.txt."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
SCRATCH = ROOT / "_audit_scratch.txt"


def _wave_patterns(name: str) -> bool:
    return name.startswith("test_wave_e") or name.startswith("test_wave_f")


def _bot_modules_from_tree(tree: ast.AST) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("bot."):
            mods.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("bot.") or alias.name == "bot":
                    mods.add(alias.name)
    return mods


def _count_assertions(tree: ast.AST) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            n += 1
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr.startswith("assert"):
                n += 1
            elif isinstance(func, ast.Name) and func.id.startswith("assert"):
                n += 1
    return n


def _load_modules(path: Path) -> tuple[set[str], int]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    return _bot_modules_from_tree(tree), _count_assertions(tree)


def _collect_non_wave_coverage() -> set[str]:
    covered: set[str] = set()
    for path in sorted(TESTS.glob("test_*.py")):
        if _wave_patterns(path.name):
            continue
        mods, _ = _load_modules(path)
        covered |= mods
    return covered


def _verdict(assertions: int, unique: set[str], all_mods: set[str]) -> str:
    if assertions < 3:
        return "CANDIDATE_FOR_DELETION"
    if all_mods and not unique:
        return "CANDIDATE_FOR_DELETION"
    if assertions > 5 and unique:
        return "KEEP"
    return "REVIEW_NEEDED"


def main() -> int:
    non_wave = _collect_non_wave_coverage()
    wave_files = sorted(
        p for p in TESTS.glob("test_*.py") if _wave_patterns(p.name)
    )
    if not wave_files:
        print("No wave e/f test files found", file=sys.stderr)
        return 1

    lines: list[str] = []
    counts = {"KEEP": 0, "CANDIDATE_FOR_DELETION": 0, "REVIEW_NEEDED": 0}

    for path in wave_files:
        mods, assertions = _load_modules(path)
        unique = mods - non_wave
        duplicate = mods & non_wave
        verdict = _verdict(assertions, unique, mods)
        counts[verdict] += 1

        lines.append(f"FILE: {path.relative_to(ROOT)}")
        lines.append(
            "UNIQUE MODULES TESTED: "
            + (", ".join(sorted(unique)) if unique else "(none)")
        )
        lines.append(
            "DUPLICATE COVERAGE: "
            + (", ".join(sorted(duplicate)) if duplicate else "(none)")
        )
        lines.append(f"ASSERTIONS COUNT: {assertions}")
        lines.append(f"VERDICT: {verdict}")
        lines.append("")

    total = len(wave_files)
    lines.append("=== SUMMARY ===")
    lines.append(f"TOTAL WAVE E/F FILES: {total}")
    lines.append(f"KEEP: {counts['KEEP']}")
    lines.append(f"CANDIDATE_FOR_DELETION: {counts['CANDIDATE_FOR_DELETION']}")
    lines.append(f"REVIEW_NEEDED: {counts['REVIEW_NEEDED']}")

    block = "\n".join(lines) + "\n"
    with SCRATCH.open("a", encoding="utf-8") as f:
        if SCRATCH.stat().st_size > 0:
            f.write("\n")
        f.write(block)

    print(f"KEEP: {counts['KEEP']}")
    print(f"CANDIDATE_FOR_DELETION: {counts['CANDIDATE_FOR_DELETION']}")
    print(f"REVIEW_NEEDED: {counts['REVIEW_NEEDED']}")
    print(f"TOTAL: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

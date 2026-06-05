#!/usr/bin/env python3
"""Detect static import cycles within the bot/ package.

Known mitigated cycles (lazy imports / TYPE_CHECKING) are whitelisted in
KNOWN_MITIGATED_CYCLES. New cycles fail the check.
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = REPO_ROOT / "bot"

# Documented in CLAUDE.md — runtime-safe via lazy imports or TYPE_CHECKING.
KNOWN_MITIGATED_CYCLES: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"bot.domain.config", "bot.domain.strategy_catalog"}),
        frozenset({"bot.domain.config", "bot.domain.contracts"}),
        frozenset({"bot.domain.schemas", "bot.delivery.contract"}),
        frozenset({"bot.runtime.health_manager", "bot.diagnostics.runtime_ops"}),
    }
)


def _module_name(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _resolve_import(_from_mod: str, node: ast.Import | ast.ImportFrom) -> set[str]:
    targets: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name
            if name == "bot" or name.startswith("bot."):
                targets.add(name)
        return targets
    if node.level or not node.module:
        return targets
    base = node.module
    if base != "bot" and not base.startswith("bot."):
        return targets
    if node.names and any(a.name == "*" for a in node.names):
        targets.add(base)
        return targets
    for alias in node.names:
        sub = alias.name.split(".")[0]
        if sub == "__init__":
            targets.add(base)
        else:
            targets.add(f"{base}.{sub}")
    return targets


def _collect_edges() -> dict[str, set[str]]:
    edges: dict[str, set[str]] = defaultdict(set)
    for py in sorted(BOT_ROOT.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        mod = _module_name(py)
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as exc:
            print(f"Syntax error in {py}: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for target in _resolve_import(mod, node):
                    if target != mod:
                        edges[mod].add(target)
    return edges


def _find_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    visited: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        on_stack.add(node)
        stack.append(node)
        for neighbor in sorted(edges.get(node, ())):
            if neighbor not in visited:
                dfs(neighbor)
            elif neighbor in on_stack:
                idx = stack.index(neighbor)
                cycle = [*stack[idx:], neighbor]
                cycles.append(cycle)
        stack.pop()
        on_stack.remove(node)

    for node in sorted(edges):
        if node not in visited:
            dfs(node)
    return cycles


def _normalize_cycle(cycle: list[str]) -> frozenset[str]:
    # Drop closing duplicate node if present.
    nodes = cycle[:-1] if len(cycle) > 1 and cycle[0] == cycle[-1] else cycle
    return frozenset(nodes)


def main() -> int:
    if not BOT_ROOT.is_dir():
        print("bot/ not found", file=sys.stderr)
        return 1
    edges = _collect_edges()
    raw_cycles = _find_cycles(edges)
    novel: list[list[str]] = []
    seen_norm: set[frozenset[str]] = set()
    for cycle in raw_cycles:
        norm = _normalize_cycle(cycle)
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        if norm in KNOWN_MITIGATED_CYCLES or any(norm <= known for known in KNOWN_MITIGATED_CYCLES):
            continue
        novel.append(cycle)

    if not novel:
        print(f"OK: no new import cycles in bot/ ({len(edges)} modules scanned)")
        return 0

    print("NEW import cycles detected:", file=sys.stderr)
    for cycle in novel[:20]:
        print("  " + " -> ".join(cycle), file=sys.stderr)
    if len(novel) > 20:
        print(f"  ... and {len(novel) - 20} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

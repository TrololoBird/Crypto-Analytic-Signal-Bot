"""CI gate: Hunter market plane must stay 100% CCXT (no raw Binance HTTP)."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

HUNT_ROOT = Path(__file__).resolve().parents[2]
CORE = HUNT_ROOT / "hunt_core"

# Raw Binance market HTTP — forbidden outside comments/docstrings.
_FORBIDDEN_URL_RE = re.compile(
    r"fapi\.binance\.com|fstream\.binance\.com|api\.binance\.com/fapi",
    re.IGNORECASE,
)
_FORBIDDEN_PATH_RE = re.compile(r"https?://[^\s\"']*binance\.com/(fapi|futures)", re.IGNORECASE)
_BINANCEUSDM_RE = re.compile(r"\bbinanceusdm\b")
_API_KEY_RE = re.compile(r"""['"]apiKey['"]\s*:|['"]secret['"]\s*:""")

# aiohttp allowed only for non-market paths.
_AIOHTTP_ALLOW = frozenset(
    {
        CORE / "market" / "network.py",
        CORE / "deliver" / "telegram.py",
        CORE / "runtime" / "telegram_commands.py",
        CORE / "_dev" / "check_ccxt.py",
    }
)

# hunt_core/market may mention fapi paths in comments (ccxt.pro depth snapshot note).
_MARKET_COMMENT_OK = CORE / "market"


def _docstring_and_comment_spans(source: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return spans
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            doc = ast.get_docstring(node, clean=False)
            if doc and hasattr(node, "body") and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    spans.append((first.value.lineno, first.value.end_lineno or first.value.lineno))
    for i, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            spans.append((i, i))
    return spans


def _in_spans(lineno: int, spans: list[tuple[int, int]]) -> bool:
    return any(lo <= lineno <= hi for lo, hi in spans)


def _scan_file(path: Path) -> list[str]:
    violations: list[str] = []
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path}: unreadable ({exc})"]
    spans = _docstring_and_comment_spans(source)
    rel = path.relative_to(HUNT_ROOT)
    for lineno, line in enumerate(source.splitlines(), start=1):
        if _in_spans(lineno, spans):
            continue
        if _FORBIDDEN_URL_RE.search(line) or _FORBIDDEN_PATH_RE.search(line):
            violations.append(f"{rel}:{lineno}: raw Binance market URL")
        if path.name != "check_ccxt.py" and _BINANCEUSDM_RE.search(line):
            violations.append(f"{rel}:{lineno}: binanceusdm forbidden")
        if path.parent == CORE / "market" and _API_KEY_RE.search(line):
            violations.append(f"{rel}:{lineno}: apiKey/secret in market plane")
    if "aiohttp" in source and path not in _AIOHTTP_ALLOW:
        if "import aiohttp" in source or "from aiohttp" in source:
            violations.append(f"{rel}: aiohttp import outside allowlist")
    return violations


def _scan_tree(root: Path) -> list[str]:
    out: list[str] = []
    if not root.is_dir():
        return out
    for py in root.rglob("*.py"):
        if "_legacy" in py.parts or "__pycache__" in py.parts:
            continue
        out.extend(_scan_file(py))
    return out


def main() -> int:
    violations: list[str] = []
    violations.extend(_scan_tree(CORE))
    print(f"ccxt canon violations: {len(violations)} ok={len(violations) == 0}")
    for line in violations[:40]:
        print(f"  {line}", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())

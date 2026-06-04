#!/usr/bin/env python3
"""Project health audit — stale files, forbidden paths, live-path safety patterns.

Run after refactors or before release:
  python scripts/project_health_audit.py
  python scripts/project_health_audit.py --stale-days 2 --json report.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SKIP_DIR_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    "graphify-out",
    "data",
    "telemetry",
    "scripts/audit_data",
    ".serena",
}

FORBIDDEN_PATHS = (
    "bot/application",
    "bot/telegram",
    "bot/websocket",
    "bot/infrastructure",
    "bot/setups/detectors",
    "bot/delivery.py",
    "bot/features.py",
    "bot/market_data.py",
    "bot/ws_manager.py",
    "bot/messaging.py",
)

FORBIDDEN_IMPORT_SNIPPETS = (
    "from bot.application",
    "import bot.application",
    "from bot.market_data",
    "from bot.ws_manager",
    "from bot.infrastructure.binance_client",
    "from bot.telegram",
    "from bot.setups.detectors",
)

LIVE_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("shift_negative_lookahead", re.compile(r"shift\s*\(\s*-\s*\d+")),
)

HOT_FEATURE_FILES = (
    "bot/features/prepare_frame.py",
    "bot/features/prepare.py",
    "bot/features/shared.py",
    "bot/features/structure.py",
    "bot/features/microstructure.py",
)


@dataclass
class AuditReport:
    generated_at: str
    stale_days: int
    stale_bot_py: list[dict[str, object]] = field(default_factory=list)
    stale_other_notable: list[dict[str, object]] = field(default_factory=list)
    large_py_files: list[dict[str, object]] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    forbidden_imports: list[str] = field(default_factory=list)
    live_path_violations: list[dict[str, str]] = field(default_factory=list)
    subprocess_checks: list[dict[str, object]] = field(default_factory=list)
    ok: bool = True

    def fail(self, message: str) -> None:
        self.ok = False
        self.subprocess_checks.append({"status": "fail", "message": message})


def _should_skip(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return any(part in SKIP_DIR_PARTS for part in parts)


def _collect_stale(root: Path, *, stale_days: int) -> list[Path]:
    cutoff = datetime.now(tz=UTC) - timedelta(days=stale_days)
    stale: list[Path] = []
    for file_path in root.rglob("*"):
        if not file_path.is_file() or _should_skip(file_path):
            continue
        mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=UTC)
        if mtime < cutoff:
            stale.append(file_path)
    return sorted(stale, key=lambda p: p.stat().st_mtime)


def _scan_forbidden_imports() -> list[str]:
    errors: list[str] = []
    for root in (REPO_ROOT / "bot", REPO_ROOT / "scripts", REPO_ROOT / "tests"):
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            if _should_skip(py):
                continue
            try:
                lines = py.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                stripped = line.strip()
                if not stripped.startswith(("from ", "import ")):
                    continue
                for snippet in FORBIDDEN_IMPORT_SNIPPETS:
                    if snippet in stripped:
                        rel = py.relative_to(REPO_ROOT)
                        errors.append(f"{rel}: forbidden import {snippet!r}")
                        break
    return errors


def _scan_live_path_patterns(files: tuple[str, ...]) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    for rel in files:
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            for label, pattern in LIVE_PATH_PATTERNS:
                if pattern.search(line):
                    violations.append(
                        {
                            "file": rel,
                            "line": str(line_no),
                            "rule": label,
                            "snippet": line.strip()[:120],
                        }
                    )
    return violations


def _collect_large_py_files(*, min_lines: int = 500) -> list[dict[str, object]]:
    """List tracked Python files exceeding ``min_lines`` (bot/, scripts/, tests/)."""
    large: list[dict[str, object]] = []
    for root_name in ("bot", "scripts", "tests"):
        root = REPO_ROOT / root_name
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            if _should_skip(py):
                continue
            try:
                line_count = sum(1 for _ in py.open(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if line_count > min_lines:
                large.append(
                    {
                        "path": str(py.relative_to(REPO_ROOT)),
                        "lines": line_count,
                    }
                )
    return sorted(large, key=lambda item: int(item["lines"]), reverse=True)


def _run_cmd(name: str, cmd: list[str]) -> dict[str, object]:
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    return {
        "name": name,
        "cmd": " ".join(cmd),
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:] if proc.stdout else "",
        "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
        "ok": proc.returncode == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stale-days", type=int, default=2, help="Flag files older than N days")
    parser.add_argument("--json", type=Path, default=None, help="Write JSON report path")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also run mypy critical + offline pytest (slower; CI runs these separately)",
    )
    args = parser.parse_args()

    report = AuditReport(
        generated_at=datetime.now(tz=UTC).isoformat(),
        stale_days=args.stale_days,
    )

    stale_all = _collect_stale(REPO_ROOT, stale_days=args.stale_days)
    for path in stale_all:
        rel = str(path.relative_to(REPO_ROOT))
        age_days = (
            datetime.now(tz=UTC) - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        ).days
        entry = {
            "path": rel,
            "age_days": age_days,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
        }
        if rel.startswith("bot") and rel.endswith(".py"):
            report.stale_bot_py.append(entry)
        elif (
            rel.startswith(("bot/", "scripts/", "tests/", "config/"))
            and not rel.endswith((".pyc", ".log", ".json"))
            and len(report.stale_other_notable) < 80
        ):
            report.stale_other_notable.append(entry)

    for rel in FORBIDDEN_PATHS:
        if (REPO_ROOT / rel).exists():
            report.forbidden_paths.append(rel)
            report.ok = False

    report.forbidden_imports = _scan_forbidden_imports()
    if report.forbidden_imports:
        report.ok = False

    report.large_py_files = _collect_large_py_files(min_lines=500)

    report.live_path_violations = _scan_live_path_patterns(HOT_FEATURE_FILES)
    if report.live_path_violations:
        report.ok = False

    checks = [
        _run_cmd("compileall", [sys.executable, "-m", "compileall", "-q", "bot", "tests"]),
        _run_cmd("dependencies", [sys.executable, "scripts/verify_dependencies.py"]),
        _run_cmd("refactor_gate", [sys.executable, "scripts/verify_refactor_gate.py"]),
        _run_cmd(
            "validate_config",
            [sys.executable, "scripts/validate_config.py", "--config", "config.toml.example"],
        ),
    ]
    if args.full:
        checks.extend(
            [
                _run_cmd("mypy_critical", [sys.executable, "scripts/run_mypy_critical.py"]),
                _run_cmd(
                    "pytest_offline",
                    [sys.executable, "-m", "pytest", "tests/", "-q", "--ignore=tests/live"],
                ),
            ]
        )
    report.subprocess_checks.extend(checks)
    if any(not item["ok"] for item in checks):
        report.ok = False

    # Human-readable summary
    print(f"=== Project health audit ({report.generated_at}) ===")
    print(f"Stale threshold: {args.stale_days} days")
    print(f"Stale bot/*.py: {len(report.stale_bot_py)}")
    for item in report.stale_bot_py:
        print(f"  - [{item['age_days']}d] {item['path']}")
    if report.large_py_files:
        print(f"Large Python files (>500 LOC): {len(report.large_py_files)}")
        for item in report.large_py_files[:25]:
            print(f"  - [{item['lines']}] {item['path']}")
    if report.forbidden_paths:
        print("FORBIDDEN PATHS:")
        for path in report.forbidden_paths:
            print(f"  - {path}")
    if report.forbidden_imports:
        print("FORBIDDEN IMPORTS:")
        for err in report.forbidden_imports[:20]:
            print(f"  - {err}")
    if report.live_path_violations:
        print("LIVE PATH VIOLATIONS (negative bar shift on live path):")
        for item in report.live_path_violations:
            print(f"  - {item['file']}:{item['line']} {item['rule']}")
    for check in checks:
        status = "OK" if check["ok"] else "FAIL"
        print(f"[{status}] {check['name']}")
        if not check["ok"] and check.get("stderr_tail"):
            print(check["stderr_tail"])

    if args.json:
        args.json.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    print("RESULT:", "PASS" if report.ok else "FAIL")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

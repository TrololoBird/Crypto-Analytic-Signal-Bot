#!/usr/bin/env python3
"""Audit requirements-lock.txt; fail on unknown CVEs (known aiohttp → SECURITY.md)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-lock.txt"

# Accepted until aiogram allows aiohttp>=3.14 — see SECURITY.md
IGNORED_VULNS = frozenset(
    {
        "CVE-2026-34993",
        "CVE-2026-47265",
    }
)


def main() -> int:
    if not LOCK.is_file():
        print(f"[FAIL] missing {LOCK}", file=sys.stderr)
        return 1

    out = ROOT / ".pip-audit-report.json"
    proc = subprocess.run(
        [
            "pip-audit",
            "-r",
            str(LOCK),
            "-f",
            "json",
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if not out.is_file():
        print(proc.stderr or proc.stdout or "[FAIL] pip-audit produced no output", file=sys.stderr)
        return proc.returncode or 1

    data = json.loads(out.read_text(encoding="utf-8"))
    out.unlink(missing_ok=True)

    blocking: list[str] = []
    ignored: list[str] = []
    for dep in data.get("dependencies", []):
        name = dep.get("name", "?")
        for vuln in dep.get("vulns") or []:
            vid = vuln.get("id", "?")
            line = f"{name} {vid}"
            if vid in IGNORED_VULNS:
                ignored.append(line)
            else:
                blocking.append(line)

    for line in ignored:
        print(f"[ignored] {line} (SECURITY.md)")

    if blocking:
        print("[FAIL] Unaccepted vulnerabilities:", file=sys.stderr)
        for line in blocking:
            print(f"  {line}", file=sys.stderr)
        return 1

    print("[OK] pip-audit: no blocking vulnerabilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

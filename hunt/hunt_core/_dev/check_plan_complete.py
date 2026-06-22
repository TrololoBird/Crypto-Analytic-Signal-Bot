"""Final gate for abstract-chasing-cerf plan — structural invariants only."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]


def _missing(path: str) -> bool:
    return not (CORE / path).exists()


def main() -> int:
    issues: list[str] = []

    removed_paths = (
        "analysis/deep_signal.py",
        "analysis/deep/__init__.py",
        "analysis/trend_engine.py",
        "analysis/adx_thresholds.py",
        "scanner/detect/deep",
        "scanner/detect/phase_compat.py",
        "scanner/gate/_phase_compat.py",
    )
    for rel in removed_paths:
        if (CORE / rel).exists():
            issues.append(f"legacy path still exists: {rel}")

    required = (
        "deep/verdict_v2/reconcile.py",
        "deep/verdict_v2/activation.py",
        "deep/plan.py",
        "deep/engine.py",
        "deep/arbiter.py",
        "shared/facts/trend.py",
        "shared/facts/adx_thresholds.py",
        "_dev/check_deep_e2e.py",
    )
    for rel in required:
        if not (CORE / rel).exists():
            issues.append(f"missing required: {rel}")

    proc = subprocess.run(
        [sys.executable, "-m", "hunt_core._dev.check_imports"],
        capture_output=True,
        text=True,
        cwd=CORE.parent,
    )
    if proc.returncode != 0:
        issues.append("check_imports failed")

    if issues:
        print(f"check_plan_complete: {len(issues)} issue(s)", file=sys.stderr)
        for line in issues:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("check_plan_complete ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

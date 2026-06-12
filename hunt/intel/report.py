"""Validate and persist intel analyst responses — suggestions only, never applies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from intel.schema import validate_intel_report
from hunt_watch.paths import INTEL_REPORT


def save_intel_report(
    report: dict[str, Any],
    *,
    path: Path = INTEL_REPORT,
) -> tuple[bool, list[str]]:
    ok, errors = validate_intel_report(report)
    if not ok:
        return False, errors
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return True, []


def load_intel_report(path: Path = INTEL_REPORT) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None

"""Paths and git helpers for the persistent forensic archive."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORENSICS_DIR = ROOT / "data" / "forensics"
FORENSIC_ARCHIVE_PATH = FORENSICS_DIR / "forensic_archive.db"
REPORT_ARCHIVE_PATH = ROOT / "REPORT_FORENSIC_ARCHIVE.md"

SL_RESULTS = frozenset({"stop_loss", "breakeven_stop", "trailing_stop"})


def git_short_hash() -> str | None:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=ROOT,
                text=True,
                stderr=subprocess.DEVNULL,
            )
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def ensure_forensics_dir() -> Path:
    FORENSICS_DIR.mkdir(parents=True, exist_ok=True)
    return FORENSICS_DIR

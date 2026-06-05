#!/usr/bin/env python3
"""Print or write status of supervised live session (visible without Cursor terminals)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS_FILE = ROOT / "LIVE_SESSION_STATUS.json"


def _find_pids() -> list[dict[str, str]]:
    import subprocess

    out: list[dict[str, str]] = []
    for pattern in ("live_supervised_session", "main.py run", "main.py"):
        proc = subprocess.run(
            ["pgrep", "-fl", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in (proc.stdout or "").strip().splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid, cmd = parts[0], parts[1]
            if "pgrep" in cmd or "live_session_status" in cmd:
                continue
            out.append({"pid": pid, "cmd": cmd, "pattern": pattern})
    return out


def _latest_run_dir() -> Path | None:
    base = ROOT / "data" / "live_watch"
    if not base.is_dir():
        return None
    dirs = [p for p in base.iterdir() if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def _tail_log(path: Path, n: int = 3) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-n:] if lines else []


def build_status() -> dict[str, object]:
    pids = _find_pids()
    run_dir = _latest_run_dir()
    log_dir = ROOT / "logs"
    latest_log = None
    if log_dir.is_dir():
        logs = sorted(log_dir.glob("live_supervised*.log"), key=lambda p: p.stat().st_mtime)
        latest_log = logs[-1] if logs else None

    status: dict[str, object] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "running": bool(pids),
        "processes": pids,
        "status_file": str(STATUS_FILE),
        "latest_live_watch": str(run_dir) if run_dir else None,
        "latest_log": str(latest_log) if latest_log else None,
    }
    if latest_log:
        status["log_tail"] = _tail_log(latest_log)
    if run_dir:
        meta = run_dir / "session_meta.json"
        if meta.is_file():
            try:
                status["session_meta"] = json.loads(meta.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                status["session_meta"] = {"error": "invalid json"}
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write LIVE_SESSION_STATUS.json at repo root",
    )
    args = parser.parse_args()
    status = build_status()
    text = json.dumps(status, indent=2, ensure_ascii=False)
    if args.write:
        STATUS_FILE.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if status.get("running") else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Hunt watch liveness check — restart if dead; optional TG alert.

  .venv/bin/python scripts/hunt_watch_health.py
  .venv/bin/python scripts/hunt_watch_health.py --restart
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUNT = ROOT / "hunt"
PID_FILE = HUNT / "data" / "watch.pid"
WATCH_LOG = HUNT / "data" / "watch_live.log"
WATCH_SH = HUNT / "scripts" / "watch.sh"


def _latest_watch_log() -> Path:
    """Prefer freshest watch log (supervisor may write watch_health_*.log)."""
    data = HUNT / "data"
    sessions = sorted((data / "sessions").glob("*/watch.log"), reverse=True) if (data / "sessions").is_dir() else []
    candidates = [WATCH_LOG, *sessions, *sorted(data.glob("watch_health_*.log"), reverse=True)]
    best: Path | None = None
    best_mtime = 0.0
    for path in candidates:
        if path.exists() and path.stat().st_mtime > best_mtime:
            best = path
            best_mtime = path.stat().st_mtime
    return best or WATCH_LOG


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _watch_running() -> bool:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            pid = 0
        if _pid_alive(pid):
            return True
    proc = subprocess.run(
        ["pgrep", "-f", "[h]unt_core watch"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def _log_stale(*, max_age_s: float) -> bool:
    log_path = _latest_watch_log()
    if not log_path.exists():
        return True
    age = time.time() - log_path.stat().st_mtime
    if age > max_age_s:
        return True
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-12000:]
    except OSError:
        return True
    markers = ("watch_tick", "watch_universe", "hunt_scan_refresh", "watch_telegram_ready")
    return not any(m in tail for m in markers)


def _supervisor_session_running() -> bool:
    proc = subprocess.run(
        ["pgrep", "-f", "[s]upervised_session.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def _restart_watch() -> None:
    if _supervisor_session_running():
        # Let supervised_session's watch.sh loop respawn — avoid duplicate bash supervisors.
        subprocess.run(["pkill", "-f", "[h]unt_core watch"], check=False)
        if PID_FILE.exists():
            PID_FILE.unlink(missing_ok=True)
        time.sleep(12)
        return
    subprocess.run(["pkill", "-f", "[h]unt_core watch"], check=False)
    if PID_FILE.exists():
        PID_FILE.unlink(missing_ok=True)
    WATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        ["bash", str(WATCH_SH)],
        cwd=str(HUNT),
        stdout=WATCH_LOG.open("a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "HUNT_WATCH_SUPERVISE": "0", "HUNT_SUPERVISED_CHILD": "1"},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hunt watch health")
    parser.add_argument("--restart", action="store_true", help="Restart watch if unhealthy")
    parser.add_argument("--max-log-age-s", type=float, default=600.0)
    args = parser.parse_args(argv)

    running = _watch_running()
    stale = _log_stale(max_age_s=args.max_log_age_s)
    healthy = running and not stale
    status = "ok" if healthy else "unhealthy"
    print(f"hunt_watch_health {status} running={running} log_stale={stale}")

    if healthy:
        return 0
    if args.restart:
        _restart_watch()
        time.sleep(8)
        ok = _watch_running()
        print(f"restart_done running={ok}")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

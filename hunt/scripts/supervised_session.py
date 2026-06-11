#!/usr/bin/env python3
"""Supervised Hunt Watch — N hours with verify_diff, pause on mismatch, auto-resume after fix.

Usage:
    python hunt/scripts/supervised_session.py --hours 6
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.monitor import run_verify_sync
from hunt_watch.paths import DATA, TICK_JSONL

ROOT = Path(__file__).resolve().parents[2]
WATCH_SCRIPT = ROOT / "hunt" / "scripts" / "watch.py"

LOG = logging.getLogger("hunt_watch.supervised")
PAUSE_RECHECK_S = 300.0


def _configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def _prep() -> None:
    subprocess.run(
        [sys.executable, "scripts/clean_session_data.py", "--mode", "smoke", "--config", "config.toml"],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [sys.executable, "scripts/validate_config.py", "--config", "config.toml"],
        cwd=ROOT,
        check=True,
    )


def _start_watch(*, watch_log: Path, interval: int) -> tuple[subprocess.Popen[bytes], object]:
    watch_log.parent.mkdir(parents=True, exist_ok=True)
    watch_out = watch_log.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(WATCH_SCRIPT), "--interval", str(interval)],
        cwd=ROOT,
        stdout=watch_out,
        stderr=subprocess.STDOUT,
    )
    return proc, watch_out


def _stop_watch(proc: subprocess.Popen[bytes] | None, watch_out: object | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    LOG.warning("hunt_watch_pausing pid=%s reason=mismatch_or_shutdown", proc.pid)
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    if watch_out is not None:
        watch_out.close()  # type: ignore[union-attr]


def _raise_exit(_signum: int, _frame: object) -> None:
    # Default SIGTERM kills the process WITHOUT running `finally` — the child
    # watch.py survived every `--stop` as an orphan. SystemExit unwinds the
    # try/finally so _stop_watch() reaps the child.
    raise SystemExit(0)


def main() -> int:
    signal.signal(signal.SIGTERM, _raise_exit)
    signal.signal(signal.SIGINT, _raise_exit)
    parser = argparse.ArgumentParser(description="Supervised Hunt Watch (pump/dump) session")
    parser.add_argument("--hours", type=float, default=6.0, help="Session duration (default 6h)")
    parser.add_argument("--watch-interval", type=int, default=60, help="Watch tick interval seconds")
    parser.add_argument("--verify-interval", type=int, default=900, help="verify_diff every N seconds")
    parser.add_argument("--verify-limit", type=int, default=15, help="Symbols per verify_diff pass")
    parser.add_argument(
        "--halt-on-mismatch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pause watch when independent analysis disagrees (default on)",
    )
    args = parser.parse_args()

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    session_dir = DATA / "sessions" / run_id
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = ROOT / "logs" / f"hunt_supervised_{run_id}.log"
    _configure_logging(log_path)

    meta = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "hours": args.hours,
        "watch_interval": args.watch_interval,
        "verify_interval": args.verify_interval,
        "halt_on_mismatch": args.halt_on_mismatch,
        "jsonl": str(TICK_JSONL),
    }
    (session_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    LOG.info("hunt_supervised_prep run_id=%s", run_id)
    _prep()

    watch_log = ROOT / "logs" / f"hunt_watch_{run_id}.log"
    watch_proc, watch_out = _start_watch(watch_log=watch_log, interval=args.watch_interval)
    LOG.info("hunt_watch_started pid=%s log=%s", watch_proc.pid, watch_log)

    end_at = time.monotonic() + args.hours * 3600.0
    verify_runs: list[dict[str, object]] = []
    next_verify = time.monotonic() + min(120.0, args.verify_interval)
    watch_paused = False
    pause_events = 0
    resume_events = 0
    exit_code = 0

    try:
        while time.monotonic() < end_at:
            if not watch_paused and watch_proc.poll() is not None:
                LOG.error("hunt_watch_exited_early code=%s", watch_proc.returncode)
                exit_code = 1
                break

            sleep_s = min(30.0, end_at - time.monotonic(), max(1.0, next_verify - time.monotonic()))
            time.sleep(sleep_s)

            if time.monotonic() < next_verify or time.monotonic() >= end_at:
                continue

            LOG.info("hunt_verify_diff_start paused=%s", watch_paused)
            result = run_verify_sync(limit=args.verify_limit, session_dir=session_dir)
            verify_runs.append(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "mismatch_count": result["mismatch_count"],
                    "severe_count": result["severe_count"],
                    "text_path": result["text_path"],
                    "alert_path": result["alert_path"],
                    "watch_paused": watch_paused,
                }
            )
            LOG.info(
                "hunt_verify_diff_done mismatches=%s severe=%s alert=%s",
                result["mismatch_count"],
                result["severe_count"],
                result["alert_path"],
            )

            if result["mismatch_count"] > 0:
                for row in result["mismatches"]:
                    LOG.warning(
                        "hunt_mismatch symbol=%s verdict=%s bot=%s/%s ind=%s note=%s",
                        row.symbol,
                        row.verdict,
                        row.bot_phase,
                        row.bot_bias,
                        row.ind_bias,
                        row.note,
                    )
                if args.halt_on_mismatch and result["severe_count"] > 0 and not watch_paused:
                    _stop_watch(watch_proc, watch_out)
                    watch_proc = None  # type: ignore[assignment]
                    watch_out = None
                    watch_paused = True
                    pause_events += 1
                    LOG.warning(
                        "hunt_watch_paused_for_repair mismatches=%s — fix code then auto-resume on clean verify",
                        result["mismatch_count"],
                    )
                    next_verify = time.monotonic() + PAUSE_RECHECK_S
                    continue
            if watch_paused and result["severe_count"] == 0:
                watch_proc, watch_out = _start_watch(watch_log=watch_log, interval=args.watch_interval)
                watch_paused = False
                resume_events += 1
                LOG.info("hunt_watch_resumed pid=%s after_clean_verify", watch_proc.pid)

            next_verify = time.monotonic() + (
                PAUSE_RECHECK_S if watch_paused else args.verify_interval
            )
    finally:
        _stop_watch(watch_proc, watch_out)

    summary = {
        **meta,
        "ended_at": datetime.now(UTC).isoformat(),
        "watch_exit_code": watch_proc.returncode if watch_proc else None,
        "verify_runs": verify_runs,
        "pause_events": pause_events,
        "resume_events": resume_events,
        "total_mismatches_last": verify_runs[-1]["mismatch_count"] if verify_runs else None,
        "ended_paused": watch_paused,
    }
    summary_path = session_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    LOG.info("hunt_supervised_done run_id=%s summary=%s", run_id, summary_path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

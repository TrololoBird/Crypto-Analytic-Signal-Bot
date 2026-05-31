"""Live bot session with per-minute snapshots and optional multi-session loop.

Usage:
    python -m scripts.live_supervised_session --minutes 16 --snapshot-interval 60
    python -m scripts.live_supervised_session --minutes 16 --hours 4 --snapshot-interval 60
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from bot.diagnostics.runtime_analysis import (
    find_latest_run_dir,
    parse_cycle_log_lines,
    read_jsonl,
)

ROOT = Path(__file__).resolve().parents[1]
LOG = structlog.get_logger("scripts.live_supervised_session")

_ERROR_MARKERS = (
    "Traceback (most recent call last)",
    "ERROR",
    "CRITICAL",
    "Exception",
    "MarketDataUnavailable",
    "strategy_error",
)


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


def _tracking_db_path() -> Path:
    for candidate in (
        ROOT / "data" / "bot" / "tracking.db",
        ROOT / "data" / "tracking.db",
    ):
        if candidate.exists():
            return candidate
    return ROOT / "data" / "bot" / "tracking.db"


def _bot_logs_dir() -> Path:
    return ROOT / "data" / "bot" / "logs"


def _pid_file() -> Path:
    return ROOT / "data" / "bot" / "bot.pid"


def _read_pid_value(pid_file: Path) -> int:
    from bot.ops.pid_utils import read_pid_file

    return read_pid_file(pid_file)


def _pid_is_alive(pid: int) -> bool:
    from bot.ops.pid_utils import pid_is_alive

    return pid_is_alive(pid)


def _stop_existing_bot(*, exclude_pids: set[int]) -> list[int]:
    """Terminate processes holding the bot PID lock (supervisor PID excluded)."""
    from bot.ops.pid_utils import stop_bot_processes

    stopped = stop_bot_processes(
        repo_root=ROOT,
        pid_file=_pid_file(),
        exclude_pids=exclude_pids,
    )
    for pid in stopped:
        LOG.warning("stopping_existing_bot", pid=pid)
    return stopped


def _latest_bot_log() -> Path | None:
    logs_dir = _bot_logs_dir()
    if not logs_dir.exists():
        return None
    candidates = [p for p in logs_dir.glob("bot_*.log") if p.is_file()]
    if not candidates:
        fallback = logs_dir / "bot.log"
        return fallback if fallback.exists() else None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _holder_bot_log() -> Path | None:
    """Resolve the active bot session log via PID lock (bot_<stamp>_<pid>.log)."""
    holder = _read_pid_value(_pid_file())
    if holder <= 0 or not _pid_is_alive(holder):
        return None
    logs_dir = _bot_logs_dir()
    if not logs_dir.exists():
        return None
    matches = sorted(
        logs_dir.glob(f"bot_*_{holder}.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _read_log_tail(path: Path, *, max_bytes: int = 256_000) -> list[str]:
    if not path.exists():
        return []
    size = path.stat().st_size
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()
        return handle.read().splitlines()


def _extract_errors(lines: list[str]) -> list[str]:
    hits: list[str] = []
    for line in lines:
        if any(marker in line for marker in _ERROR_MARKERS):
            hits.append(line.strip()[:500])
    return hits[-30:]


def _fetch_tracking_summary(conn: sqlite3.Connection, since_iso: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    summary: dict[str, Any] = {
        "active_count": 0,
        "closed_since": 0,
        "delivered_since": 0,
        "recent": [],
    }
    try:
        summary["active_count"] = int(
            conn.execute("SELECT COUNT(*) FROM active_signals WHERE closed_at IS NULL").fetchone()[0]
        )
        summary["closed_since"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM active_signals WHERE closed_at >= ?",
                (since_iso,),
            ).fetchone()[0]
        )
        rows = conn.execute(
            """
            SELECT tracking_id, symbol, setup_id, direction, score, created_at, closed_at, close_reason
            FROM active_signals
            WHERE created_at >= ? OR closed_at >= ?
            ORDER BY COALESCE(closed_at, created_at) DESC
            LIMIT 20
            """,
            (since_iso, since_iso),
        ).fetchall()
        summary["recent"] = [dict(row) for row in rows]
    except sqlite3.OperationalError as exc:
        summary["db_error"] = str(exc)
    return summary


def _telemetry_snapshot(telemetry_dir: Path) -> dict[str, Any]:
    run_dir = find_latest_run_dir(telemetry_dir)
    if run_dir is None:
        return {"run_dir": None}
    analysis = run_dir / "analysis"
    out: dict[str, Any] = {"run_dir": str(run_dir)}
    for name in ("cycles.jsonl", "signals.jsonl", "rejected.jsonl", "strategy_decisions.jsonl"):
        path = analysis / name
        rows = read_jsonl(path) if path.exists() else []
        out[name] = {"rows": len(rows), "tail": rows[-3:] if rows else []}
    return out


def _strategy_error_lines(lines: list[str]) -> list[str]:
    pattern = re.compile(r"calculate_all complete.*errors=(\d+)")
    hits: list[str] = []
    for line in lines:
        if "errors=" in line and "calculate_all" in line:
            match = pattern.search(line)
            if match and int(match.group(1)) > 0:
                hits.append(line.strip()[:400])
        if "strategy_error" in line.lower() or "AttributeError" in line:
            hits.append(line.strip()[:400])
    return hits[-20:]


async def _snapshot_loop(
    *,
    snap_log: Path,
    bot_log: Path,
    end_at: float,
    interval: float,
    session_start: datetime,
    stdout_offset: list[int],
    log_offsets: dict[str, int],
) -> None:
    tick = 0
    since_iso = session_start.isoformat()
    telemetry_dir = ROOT / "data" / "telemetry"
    totals = {
        "cycles": 0,
        "candidates_total": 0,
        "delivered_total": 0,
        "rejected_total": 0,
        "detector_runs_total": 0,
        "symbols_processed": set(),
    }
    while time.time() < end_at:
        tick += 1
        now = datetime.now(timezone.utc)
        lines: list[str] = []
        session_log: Path | None = None
        if bot_log.exists():
            size = bot_log.stat().st_size
            offset = stdout_offset[0]
            if size < offset:
                offset = 0
            with bot_log.open("r", encoding="utf-8", errors="ignore") as handle:
                handle.seek(offset)
                chunk = handle.read()
                stdout_offset[0] = handle.tell()
            lines = chunk.splitlines()
        holder_log = _holder_bot_log()
        session_log = holder_log or _latest_bot_log()
        if session_log is not None and session_log != bot_log:
            key = str(session_log)
            offset = log_offsets.get(key, 0)
            size = session_log.stat().st_size
            if size < offset:
                offset = 0
            with session_log.open("r", encoding="utf-8", errors="ignore") as handle:
                handle.seek(offset)
                chunk = handle.read()
                log_offsets[key] = handle.tell()
            if chunk:
                lines.extend(chunk.splitlines())

        parsed = parse_cycle_log_lines(lines)
        totals["cycles"] += parsed["cycles"]
        totals["candidates_total"] += parsed["candidates_total"]
        totals["delivered_total"] += parsed["delivered_total"]
        totals["rejected_total"] += parsed["rejected_total"]
        totals["detector_runs_total"] += parsed["detector_runs_total"]
        totals["symbols_processed"].update(parsed["symbols_processed"])
        tracking: dict[str, Any] = {"db": str(_tracking_db_path()), "exists": _tracking_db_path().exists()}
        if tracking["exists"]:
            with sqlite3.connect(_tracking_db_path()) as conn:
                tracking["summary"] = _fetch_tracking_summary(conn, since_iso)

        payload = {
            "tick": tick,
            "utc": now.isoformat(),
            "elapsed_s": round((now - session_start).total_seconds(), 1),
            "bot_stdout": str(bot_log),
            "session_log": str(session_log) if session_log else None,
            "holder_pid": _read_pid_value(_pid_file()) or None,
            "holder_alive": _pid_is_alive(_read_pid_value(_pid_file())),
            "runtime": {
                "cycles_delta": parsed["cycles"],
                "candidates_delta": parsed["candidates_total"],
                "delivered_delta": parsed["delivered_total"],
                "rejected_delta": parsed["rejected_total"],
                "detector_runs_delta": parsed["detector_runs_total"],
                "cycles_total": totals["cycles"],
                "candidates_total": totals["candidates_total"],
                "delivered_total": totals["delivered_total"],
                "rejected_total": totals["rejected_total"],
                "detector_runs_total": totals["detector_runs_total"],
                "symbols": sorted(totals["symbols_processed"]),
                "last_signals": parsed["last_signals"][-5:],
                "log_errors": _extract_errors(lines),
                "strategy_errors": _strategy_error_lines(lines),
            },
            "tracking": tracking,
            "telemetry": _telemetry_snapshot(telemetry_dir),
        }
        snap_log.parent.mkdir(parents=True, exist_ok=True)
        with snap_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")
        LOG.info(
            "minute_snapshot",
            tick=tick,
            cycles_total=totals["cycles"],
            delivered_total=totals["delivered_total"],
            errors=len(payload["runtime"]["log_errors"]),
            strategy_errors=len(payload["runtime"]["strategy_errors"]),
        )
        await asyncio.sleep(interval)


def _stdout_has_pid_conflict(bot_log: Path) -> int | None:
    if not bot_log.exists():
        return None
    tail = _read_log_tail(bot_log, max_bytes=32_000)
    for line in reversed(tail):
        if "another bot process is already running with pid" not in line:
            continue
        for token in line.split():
            if token.isdigit():
                return int(token)
        break
    return None


async def _wait_for_holder(end_at: float, holder_pid: int) -> int:
    LOG.info("bot_monitor_holder", holder_pid=holder_pid)
    while time.time() < end_at and _pid_is_alive(holder_pid):
        await asyncio.sleep(5.0)
    return 0 if time.time() >= end_at else int(holder_pid)


async def _run_bot(
    end_at: float,
    bot_log: Path,
    restart_delay: float,
    *,
    allow_takeover: bool,
) -> int:
    del restart_delay  # retained for CLI compatibility
    holder = _read_pid_value(_pid_file())
    if holder and not _pid_is_alive(holder):
        LOG.warning("stale_pid_lock_cleared", holder_pid=holder)
        try:
            _pid_file().unlink()
        except OSError:
            pass
        holder = 0
    if holder and _pid_is_alive(holder):
        LOG.info("bot_reuse_existing_holder", holder_pid=holder)
        return await _wait_for_holder(end_at, holder)

    cmd = [sys.executable, str(ROOT / "main.py")]
    if allow_takeover:
        from bot.ops.pid_utils import clear_stale_pid_file, find_bot_main_pids

        stopped = _stop_existing_bot(exclude_pids={os.getpid()})
        if stopped:
            LOG.info("prestart_stopped_pids", pids=stopped)
        deadline = time.time() + 15.0
        while time.time() < deadline:
            clear_stale_pid_file(_pid_file())
            orphans = [
                p
                for p in find_bot_main_pids(ROOT)
                if p != os.getpid() and _pid_is_alive(p)
            ]
            holder = _read_pid_value(_pid_file())
            if not orphans and (holder <= 0 or not _pid_is_alive(holder)):
                break
            if orphans:
                _stop_existing_bot(exclude_pids={os.getpid()})
            await asyncio.sleep(1.0)
        await asyncio.sleep(2.0)

    LOG.info("bot_start", cmd=" ".join(cmd))
    with bot_log.open("a", encoding="utf-8") as out:
        out.write(f"\n--- start {datetime.now(timezone.utc).isoformat()} ---\n")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=out,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
    try:
        while proc.poll() is None and time.time() < end_at:
            await asyncio.sleep(2.0)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        last_code = proc.returncode or 0
    except Exception as exc:
        LOG.exception("bot_supervisor_error", error=str(exc))
        last_code = 1

    ended_on_schedule = time.time() >= end_at
    if ended_on_schedule:
        return 0

    holder = _stdout_has_pid_conflict(bot_log) or _read_pid_value(_pid_file())
    if holder and _pid_is_alive(holder):
        return await _wait_for_holder(end_at, holder)

    if last_code != 0:
        LOG.error("bot_exited_early", exit_code=last_code)
    return last_code


async def _run_one_session(args: argparse.Namespace, session_index: int) -> dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if session_index > 0:
        run_id = f"{run_id}_s{session_index}"
    log_dir = ROOT / "data" / "live_watch" / run_id
    bot_log = log_dir / "bot_stdout.log"
    snap_log = log_dir / "snapshots.jsonl"
    supervisor_log = log_dir / "supervisor.log"
    _configure_logging(supervisor_log)

    session_seconds = float(args.minutes) * 60.0
    end_at = time.time() + session_seconds
    session_start = datetime.now(timezone.utc)

    LOG.info(
        "session_start",
        session_index=session_index,
        minutes=args.minutes,
        log_dir=str(log_dir),
        snapshot_interval=args.snapshot_interval,
    )

    allow_takeover = bool(args.takeover) and session_index == 0
    if allow_takeover:
        stopped = _stop_existing_bot(exclude_pids={os.getpid()})
        if stopped:
            LOG.info("takeover_stopped_pids", pids=stopped)
        await asyncio.sleep(5.0)

    results = await asyncio.gather(
        _run_bot(
            end_at,
            bot_log,
            float(args.restart_delay),
            allow_takeover=allow_takeover,
        ),
        _snapshot_loop(
            snap_log=snap_log,
            bot_log=bot_log,
            end_at=end_at,
            interval=float(args.snapshot_interval),
            session_start=session_start,
            stdout_offset=[0],
            log_offsets={},
        ),
    )
    bot_exit = results[0] if results else 0
    summary_path = log_dir / "session_summary.json"
    all_snaps = read_jsonl(snap_log)
    total_errors = sum(len(s.get("runtime", {}).get("log_errors", [])) for s in all_snaps)
    total_strategy_errors = sum(
        len(s.get("runtime", {}).get("strategy_errors", [])) for s in all_snaps
    )
    last = all_snaps[-1] if all_snaps else {}
    summary = {
        "run_id": run_id,
        "session_index": session_index,
        "started_at": session_start.isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "minutes": args.minutes,
        "snapshots": len(all_snaps),
        "bot_exit_code": bot_exit,
        "total_log_errors_seen": total_errors,
        "total_strategy_error_lines": total_strategy_errors,
        "last_snapshot": last,
        "log_dir": str(log_dir),
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    LOG.info("session_finished", **{k: v for k, v in summary.items() if k != "last_snapshot"})
    return summary


async def _main_async(args: argparse.Namespace) -> int:
    outer_end = time.time() + float(args.hours) * 3600.0 if args.hours > 0 else None
    session_index = 0
    summaries: list[dict[str, Any]] = []

    while True:
        summary = await _run_one_session(args, session_index)
        summaries.append(summary)
        session_index += 1

        if outer_end is None or time.time() >= outer_end:
            break
        if time.time() + float(args.minutes) * 60.0 > outer_end:
            break
        LOG.info("session_gap", sleep_seconds=args.gap_seconds)
        await asyncio.sleep(float(args.gap_seconds))

    rollup = ROOT / "data" / "live_watch" / f"rollup_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    rollup.parent.mkdir(parents=True, exist_ok=True)
    rollup.write_text(json.dumps({"sessions": summaries}, indent=2, default=str), encoding="utf-8")
    LOG.info("supervisor_finished", sessions=len(summaries), rollup=str(rollup))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Supervised live bot with minute snapshots")
    parser.add_argument("--minutes", type=float, default=16.0, help="Bot run length per session")
    parser.add_argument("--hours", type=float, default=0.0, help="Repeat sessions until elapsed (0=once)")
    parser.add_argument("--snapshot-interval", type=float, default=60.0)
    parser.add_argument("--restart-delay", type=float, default=15.0)
    parser.add_argument("--gap-seconds", type=float, default=30.0, help="Pause between sessions")
    parser.add_argument(
        "--takeover",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stop existing bot processes only before the first session",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()

"""Long-running live bot with error/SL monitoring and auto-restart.

Usage:
    python -m scripts.live_run_watchdog --hours 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

ROOT = Path(__file__).resolve().parents[1]
LOG = structlog.get_logger("scripts.live_run_watchdog")


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


def _fetch_recent_stop_losses(conn: sqlite3.Connection, since_iso: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT tracking_id, symbol, setup_id, direction, stop_loss, close_price,
                   close_reason, closed_at, entry_low, entry_high, score
            FROM active_signals
            WHERE close_reason = 'stop_loss' AND closed_at >= ?
            ORDER BY closed_at DESC
            """,
            (since_iso,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def _analyze_sl_row(row: dict[str, Any]) -> dict[str, Any]:
    entry_mid = None
    try:
        entry_mid = (float(row.get("entry_low") or 0) + float(row.get("entry_high") or 0)) / 2.0
    except (TypeError, ValueError):
        entry_mid = None
    stop = row.get("stop_loss")
    close = row.get("close_price")
    notes: list[str] = []
    if entry_mid and stop and close:
        risk = abs(entry_mid - float(stop))
        move = abs(float(close) - entry_mid)
        if risk > 0 and move < risk * 0.5:
            notes.append("fast_stop: price barely moved before SL")
        if risk > 0 and move > risk * 2.5:
            notes.append("late_stop: large adverse move before SL tag")
    return {
        "tracking_id": row.get("tracking_id"),
        "symbol": row.get("symbol"),
        "setup_id": row.get("setup_id"),
        "direction": row.get("direction"),
        "score": row.get("score"),
        "entry_mid": entry_mid,
        "stop_loss": stop,
        "close_price": close,
        "closed_at": row.get("closed_at"),
        "notes": notes,
    }


async def _monitor_sl_loop(
    *,
    sl_log: Path,
    poll_seconds: float,
    end_at: float,
    seen: set[str],
) -> None:
    db = _tracking_db_path()
    while time.time() < end_at:
        if db.exists():
            since = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
            with sqlite3.connect(db) as conn:
                for row in _fetch_recent_stop_losses(conn, since):
                    tid = str(row.get("tracking_id") or "")
                    if not tid or tid in seen:
                        continue
                    seen.add(tid)
                    analysis = _analyze_sl_row(row)
                    sl_log.parent.mkdir(parents=True, exist_ok=True)
                    with sl_log.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(analysis, default=str) + "\n")
                    LOG.warning("stop_loss_hit", **analysis)
        await asyncio.sleep(poll_seconds)


async def _run_bot_subprocess(
    *,
    end_at: float,
    restart_delay: float,
    bot_log: Path,
) -> None:
    cmd = [sys.executable, str(ROOT / "main.py")]
    env = os.environ.copy()
    while time.time() < end_at:
        LOG.info("bot_start", cmd=" ".join(cmd))
        with bot_log.open("a", encoding="utf-8") as out:
            out.write(f"\n--- start {datetime.now(timezone.utc).isoformat()} ---\n")
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=out,
                stderr=subprocess.STDOUT,
                env=env,
            )
        try:
            while proc.poll() is None and time.time() < end_at:
                await asyncio.sleep(5.0)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception as exc:
            LOG.exception("bot_supervisor_error", error=str(exc))
        if time.time() >= end_at:
            break
        LOG.warning("bot_restart", delay_seconds=restart_delay, exit_code=proc.returncode)
        await asyncio.sleep(restart_delay)


async def _main_async(args: argparse.Namespace) -> int:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = ROOT / "data" / "live_watch" / run_id
    bot_log = log_dir / "bot_stdout.log"
    sl_log = log_dir / "stop_loss_events.jsonl"
    watchdog_log = log_dir / "watchdog.log"
    _configure_logging(watchdog_log)

    end_at = time.time() + float(args.hours) * 3600.0
    seen_sl: set[str] = set()
    LOG.info(
        "watchdog_start",
        hours=args.hours,
        log_dir=str(log_dir),
        tracking_db=str(_tracking_db_path()),
    )

    await asyncio.gather(
        _run_bot_subprocess(end_at=end_at, restart_delay=float(args.restart_delay), bot_log=bot_log),
        _monitor_sl_loop(
            sl_log=sl_log,
            poll_seconds=float(args.sl_poll_seconds),
            end_at=end_at,
            seen=seen_sl,
        ),
    )
    LOG.info("watchdog_finished", log_dir=str(log_dir))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="10h live bot supervisor")
    parser.add_argument("--hours", type=float, default=10.0)
    parser.add_argument("--restart-delay", type=float, default=15.0)
    parser.add_argument("--sl-poll-seconds", type=float, default=30.0)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()

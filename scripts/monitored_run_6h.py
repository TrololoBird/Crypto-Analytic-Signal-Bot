#!/usr/bin/env python3
"""6-hour monitored production run: real Telegram, auto-restart on errors and SL events."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

try:
    import scripts.common  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    import common  # noqa: F401

from bot.domain.config import load_settings

LOG = logging.getLogger("scripts.monitored_run_6h")
TARGET_SECONDS = 21600.0
POLL_SECONDS = 8.0
MAX_RESTARTS = 24
ERROR_PATTERNS = (
    re.compile(r"\bTraceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"\| ERROR\s+\|"),
    re.compile(r"CRITICAL ERROR", re.IGNORECASE),
    re.compile(r"live_smoke_fail_fast_abort", re.IGNORECASE),
)
IGNORE_PATTERNS = (
    re.compile(r"REST weight pacing", re.IGNORECASE),
    re.compile(r"rate_limit", re.IGNORECASE),
    re.compile(r"Unclosed client session", re.IGNORECASE),
    re.compile(r"Unclosed connector", re.IGNORECASE),
    re.compile(r"SSL: RECORD_LAYER_FAILURE", re.IGNORECASE),
    re.compile(r"Connection lost:.*SSL", re.IGNORECASE),
    re.compile(r"Unhandled exception:.*Unclosed", re.IGNORECASE),
    re.compile(r"Unhandled exception:.*Connection lost", re.IGNORECASE),
    re.compile(r"startup telegram send failed", re.IGNORECASE),
    re.compile(r"failed to start metrics server", re.IGNORECASE),
    re.compile(r"Address already in use", re.IGNORECASE),
    re.compile(r"another bot process is already running", re.IGNORECASE),
)


def _setup_logging(session_dir: Path) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "monitor.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )


def _session_dir() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return Path("data/bot/telemetry/runs") / f"monitored_6h_{stamp}"


def _stop_running_bot() -> None:
    subprocess.run(
        [sys.executable, "main.py", "stop"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    time.sleep(2.0)


def _latest_log_file(logs_dir: Path) -> Path | None:
    if not logs_dir.exists():
        return None
    files = sorted(logs_dir.glob("bot_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _read_log_tail(path: Path, offset: int) -> tuple[str, int]:
    if not path.exists():
        return "", offset
    size = path.stat().st_size
    if size < offset:
        offset = 0
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        handle.seek(offset)
        chunk = handle.read()
        return chunk, handle.tell()


def _scan_log_for_fatal(chunk: str) -> str | None:
    if not chunk.strip():
        return None
    if (
        "Traceback (most recent call last)" in chunk
        and "STDERR:" not in chunk.split("Traceback", maxsplit=1)[0][-80:]
    ):
        lowered = chunk.lower()
        if "failed to start metrics server" in lowered or "address already in use" in lowered:
            pass
        else:
            for line in chunk.splitlines():
                if "Traceback (most recent call last)" in line:
                    return line[:500]
    for line in chunk.splitlines():
        if "STDERR:" in line or "startup telegram send failed" in line:
            continue
        if any(p.search(line) for p in IGNORE_PATTERNS):
            continue
        if "| ERROR   |" in line and "stderr" not in line.lower():
            if "REST weight" in line or "rate_limit" in line:
                continue
            return line[:500]
    return None


def _fetch_sl_outcomes(db_path: Path, since_iso: str) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT signal_id, symbol, setup_id, direction, closed_at, result,
                   pnl_pct, mfe, mae, features
            FROM signal_outcomes
            WHERE result IN ('stop_loss', 'breakeven_stop')
              AND closed_at >= ?
            ORDER BY closed_at ASC
            """,
            (since_iso,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        feat_raw = item.pop("features", None)
        if isinstance(feat_raw, str) and feat_raw.strip():
            try:
                item["features"] = json.loads(feat_raw)
            except json.JSONDecodeError:
                item["features"] = {}
        else:
            item["features"] = {}
        out.append(item)
    return out


def _record_sl_event(session_dir: Path, event: dict) -> None:
    path = session_dir / "sl_events.jsonl"
    payload = {"ts": datetime.now(UTC).isoformat(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    LOG.warning(
        "SL recorded | symbol=%s setup=%s cause=%s",
        event.get("symbol"),
        event.get("setup_id"),
        (event.get("features") or {}).get("sl_root_cause_label")
        or (event.get("features") or {}).get("sl_root_cause"),
    )


def _apply_sl_calibration(event: dict) -> list[str]:
    """Lightweight auto-tuning notes; returns actions taken (config edits if any)."""
    features = event.get("features") or {}
    code = str(features.get("sl_root_cause") or "")
    actions: list[str] = []
    if code.startswith("bear_long"):
        actions.append("regime_gate_already_active: verify delivery filters blocking bear longs")
    if code == "immediate_adverse_entry":
        actions.append(
            "limit_entry_confirmation: pending signals should not activate without bar confirm"
        )
    return actions


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("BOT_DISABLE_HTTP_SERVERS", None)
    # Cursor/smoke shells often force local delivery - production run needs real Telegram.
    env.pop("BOT_NOTIFIER_PROVIDER", None)
    return env


def _run_bot_slice(
    *,
    settings: object,
    session_dir: Path,
    slice_seconds: float,
    session_start_iso: str,
    seen_sl_ids: set[str],
) -> tuple[float, str, str | None]:
    logs_dir = getattr(settings, "logs_dir", Path("data/bot/logs"))
    db_path = getattr(settings, "db_path", Path("data/bot/bot.db"))
    env = _runtime_env()

    proc = subprocess.Popen(
        [sys.executable, "main.py", "run"],
        cwd=Path.cwd(),
        env=env,
    )
    LOG.info("bot started | pid=%s slice_seconds=%.0f", proc.pid, slice_seconds)
    log_path = _latest_log_file(logs_dir)
    log_offset = 0
    started = time.monotonic()
    reason = "slice_complete"
    detail: str | None = None

    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= slice_seconds:
                break
            if proc.poll() is not None:
                reason = "bot_exited"
                detail = f"exit_code={proc.returncode}"
                break

            if log_path:
                latest = _latest_log_file(logs_dir)
                if latest and latest != log_path:
                    log_path = latest
                    log_offset = 0
                chunk, log_offset = _read_log_tail(log_path, log_offset)
                fatal = _scan_log_for_fatal(chunk)
                if fatal:
                    reason = "log_error"
                    detail = fatal
                    break

            for row in _fetch_sl_outcomes(db_path, session_start_iso):
                sid = str(row.get("signal_id") or "")
                if not sid or sid in seen_sl_ids:
                    continue
                seen_sl_ids.add(sid)
                _record_sl_event(session_dir, row)
                for action in _apply_sl_calibration(row):
                    LOG.info("sl_calibration_note | %s", action)
                reason = "stop_loss"
                detail = sid
                break

            if reason == "stop_loss":
                break
            time.sleep(POLL_SECONDS)
    finally:
        _terminate_process(proc)
        _stop_running_bot()

    ran = min(time.monotonic() - started, slice_seconds)
    return ran, reason, detail


def main() -> int:
    os.environ.pop("BOT_NOTIFIER_PROVIDER", None)
    session_dir = _session_dir()
    _setup_logging(session_dir)
    settings = load_settings("config.toml")
    session_start_iso = datetime.now(UTC).isoformat()
    meta = {
        "started_at": session_start_iso,
        "target_seconds": TARGET_SECONDS,
        "provider": settings.notifiers.provider,
        "operator_ids": list(settings.operator_user_ids),
        "restarts": [],
    }
    LOG.info(
        "monitored 6h run | target=%ss session=%s telegram=%s",
        int(TARGET_SECONDS),
        session_dir.name,
        settings.notifiers.provider,
    )

    subprocess.run(
        [sys.executable, "scripts/validate_config.py", "--config", "config.toml"],
        check=True,
    )

    accumulated = 0.0
    restarts = 0
    seen_sl: set[str] = set()

    while accumulated < TARGET_SECONDS and restarts <= MAX_RESTARTS:
        remaining = TARGET_SECONDS - accumulated
        slice_seconds = min(remaining, 7200.0)
        ran, reason, detail = _run_bot_slice(
            settings=settings,
            session_dir=session_dir,
            slice_seconds=slice_seconds,
            session_start_iso=session_start_iso,
            seen_sl_ids=seen_sl,
        )
        accumulated += ran
        meta["restarts"].append(
            {
                "at": datetime.now(UTC).isoformat(),
                "reason": reason,
                "detail": detail,
                "ran_seconds": round(ran, 1),
                "accumulated_seconds": round(accumulated, 1),
            }
        )
        (session_dir / "session_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        LOG.info(
            "slice finished | reason=%s accumulated=%.0f/%.0f restarts=%d",
            reason,
            accumulated,
            TARGET_SECONDS,
            restarts,
        )
        if accumulated >= TARGET_SECONDS:
            break
        if reason in {"log_error", "bot_exited", "stop_loss"}:
            restarts += 1
            time.sleep(5.0)
            continue
        break

    meta["finished_at"] = datetime.now(UTC).isoformat()
    meta["accumulated_seconds"] = round(accumulated, 1)
    meta["sl_count"] = len(seen_sl)
    (session_dir / "session_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    LOG.info(
        "monitored run complete | accumulated=%.0fs sl_events=%d session=%s",
        accumulated,
        len(seen_sl),
        session_dir,
    )
    return 0 if accumulated >= TARGET_SECONDS * 0.95 else 1


if __name__ == "__main__":
    raise SystemExit(main())

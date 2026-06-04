"""Resolve supervised live_watch sessions and summarize snapshot telemetry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bot.diagnostics.runtime_analysis import file_has_rows, find_latest_run_dir


def find_live_watch_session(
    live_watch_root: Path,
    run_id: str,
) -> Path | None:
    """Return session directory ``live_watch/{run_id}`` when present."""
    if not run_id:
        return None
    direct = live_watch_root / run_id
    if direct.is_dir():
        return direct
    for path in sorted(live_watch_root.glob(f"{run_id}*"), reverse=True):
        if path.is_dir():
            return path
    return None


def find_latest_rollup(live_watch_root: Path) -> Path | None:
    rollups = sorted(live_watch_root.glob("rollup_*.json"), reverse=True)
    return rollups[0] if rollups else None


def resolve_telemetry_analysis_dir(
    *,
    run_id: str,
    telemetry_dir: Path,
    live_watch_dir: Path | None = None,
) -> tuple[Path | None, str]:
    """Prefer ``telemetry/runs/{id}/analysis``; else None (use live_watch summary)."""
    runs_dir = telemetry_dir / "runs"
    if run_id:
        explicit = runs_dir / run_id / "analysis"
        if explicit.is_dir() and any(
            file_has_rows(explicit / name)
            for name in (
                "strategy_decisions.jsonl",
                "symbol_analysis.jsonl",
                "rejected.jsonl",
                "cycles.jsonl",
            )
        ):
            return explicit, "telemetry"
    latest = find_latest_run_dir(telemetry_dir, run_id=run_id or None)
    if latest is not None:
        analysis = latest / "analysis"
        if analysis.is_dir():
            return analysis, "telemetry"
    if live_watch_dir is not None and run_id:
        session = find_live_watch_session(live_watch_dir, run_id)
        if session is not None:
            return None, "live_watch"
    return None, "none"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def summarize_live_watch_session(session_dir: Path) -> dict[str, Any]:
    """Aggregate totals from ``session_summary.json`` and last snapshot."""
    summary_path = session_dir / "session_summary.json"
    session_meta: dict[str, Any] = {}
    if summary_path.is_file():
        try:
            session_meta = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            session_meta = {}

    snapshots = _read_jsonl(session_dir / "snapshots.jsonl")
    last_snap = snapshots[-1] if snapshots else {}
    runtime = dict(last_snap.get("runtime") or {})
    tracking = dict(last_snap.get("tracking") or {})

    return {
        "source": "live_watch",
        "session_dir": str(session_dir),
        "run_id": session_meta.get("run_id") or session_dir.name,
        "started_at": session_meta.get("started_at"),
        "ended_at": session_meta.get("ended_at"),
        "minutes": session_meta.get("minutes"),
        "snapshots": session_meta.get("snapshots") or len(snapshots),
        "bot_exit_code": session_meta.get("bot_exit_code"),
        "total_log_errors_seen": session_meta.get("total_log_errors_seen"),
        "total_strategy_error_lines": session_meta.get("total_strategy_error_lines"),
        "cycles_total": runtime.get("cycles_total"),
        "candidates_total": runtime.get("candidates_total"),
        "delivered_total": runtime.get("delivered_total"),
        "rejected_total": runtime.get("rejected_total"),
        "detector_runs_total": runtime.get("detector_runs_total"),
        "symbols_processed_count": len(runtime.get("symbols") or []),
        "tracking_db": tracking.get("db"),
        "decision_rows": 0,
        "strategies_ran": 0,
        "note": "Per-strategy JSONL not stored under live_watch; use telemetry/runs when available.",
    }


def summarize_rollup(rollup_path: Path) -> dict[str, Any]:
    payload = json.loads(rollup_path.read_text(encoding="utf-8"))
    sessions = list(payload.get("sessions") or [])
    totals = {
        "session_count": len(sessions),
        "delivered_total": 0,
        "cycles_total": 0,
        "snapshots": 0,
        "strategy_error_lines": 0,
    }
    session_rows: list[dict[str, Any]] = []
    for session in sessions:
        last = session.get("last_snapshot") or {}
        runtime = last.get("runtime") or {}
        delivered = int(runtime.get("delivered_total") or 0)
        cycles = int(runtime.get("cycles_total") or 0)
        totals["delivered_total"] += delivered
        totals["cycles_total"] = max(totals["cycles_total"], cycles)
        totals["snapshots"] += int(session.get("snapshots") or 0)
        totals["strategy_error_lines"] += int(session.get("total_strategy_error_lines") or 0)
        session_rows.append(
            {
                "run_id": session.get("run_id"),
                "minutes": session.get("minutes"),
                "bot_exit_code": session.get("bot_exit_code"),
                "delivered_total": delivered,
                "cycles_total": cycles,
                "snapshots": session.get("snapshots"),
            }
        )
    return {
        "rollup_path": str(rollup_path),
        "generated_at": payload.get("generated_at"),
        "totals": totals,
        "sessions": session_rows,
    }

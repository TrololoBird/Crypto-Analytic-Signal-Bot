"""Phase-0 session forensics: logs, telemetry JSONL, SQLite outcomes.

Usage:
  python scripts/forensic_session.py
  python scripts/forensic_session.py --log data/bot/logs/bot_*.log
  python scripts/forensic_session.py --run-id 20260529_180306_10668
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from bot.domain.config import load_settings


def _read_pid(data_dir: Path) -> int | None:
    pid_file = data_dir / "bot.pid"
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (TypeError, ValueError, OSError):
        return None


def _latest_log(logs_dir: Path, pid: int | None) -> Path | None:
    if not logs_dir.is_dir():
        return None
    candidates = sorted(logs_dir.glob("bot_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if pid is not None:
        for path in candidates:
            if path.stem.endswith(f"_{pid}"):
                return path
    return candidates[0] if candidates else None


def _grep_log(path: Path, needles: tuple[str, ...]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        for needle in needles:
            if needle in line:
                counts[needle] += 1
    return dict(counts)


def _latest_run_dir(runs_dir: Path, run_id: str | None) -> Path | None:
    if not runs_dir.is_dir():
        return None
    if run_id:
        explicit = runs_dir / run_id
        return explicit if explicit.is_dir() else None
    runs = sorted(
        (p for p in runs_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return runs[0] if runs else None


def _analyze_jsonl(path: Path, *, key: str, limit: int = 5000) -> Counter[str]:
    counts: Counter[str] = Counter()
    if not path.is_file():
        return counts
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for raw_line in lines[-limit:]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            val = row.get(key) or row.get("reason") or row.get("reason_code") or row.get("stage")
            if val:
                counts[str(val)] += 1
    return counts


def _sqlite_summary(db_path: Path) -> dict[str, Any]:
    if not db_path.is_file():
        return {"error": "db_missing"}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    out: dict[str, Any] = {}
    try:
        out["active_signals"] = conn.execute(
            "SELECT COUNT(*) FROM active_signals WHERE status != 'closed'"
        ).fetchone()[0]
        out["signal_outcomes_total"] = conn.execute(
            "SELECT COUNT(*) FROM signal_outcomes"
        ).fetchone()[0]
        out["outcomes_by_result"] = dict(
            conn.execute(
                "SELECT result, COUNT(*) FROM signal_outcomes GROUP BY result ORDER BY 2 DESC"
            ).fetchall()
        )
    finally:
        conn.close()
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Forensic session report")
    parser.add_argument("--log", type=Path, default=None, help="Explicit log file")
    parser.add_argument("--run-id", default=None, help="Telemetry run id folder name")
    args = parser.parse_args()

    settings = load_settings()
    data_dir = settings.data_dir
    pid = _read_pid(data_dir)
    log_path = args.log or _latest_log(settings.logs_dir, pid)
    run_dir = _latest_run_dir(settings.telemetry_dir / "runs", args.run_id)

    print("=== SESSION FORENSICS ===")
    print(f"data_dir={data_dir}")
    print(f"pid_file={pid}")
    print(f"log={log_path}")
    print(f"telemetry_run={run_dir}")

    needles = (
        "calculate_all skipped",
        "no strategy_fits",
        "calculate_all called",
        "local signal logged",
        "telegram message sent",
        "notifier provider is not telegram",
        "notifier provider is disabled",
        "delivery preflight",
        "shortlist refresh failed",
        "pinned_fallback",
        "strategy not routed",
        "asset_fit.shortlist_not_routed",
        "cycle_timeout",
        "dashboard server disabled",
        "Unhandled exception",
    )
    if log_path and log_path.is_file():
        print("\n--- LOG GREP ---")
        for key, count in sorted(_grep_log(log_path, needles).items(), key=lambda x: -x[1]):
            print(f"  {count:5d}  {key}")
    else:
        print("\n--- LOG GREP --- (no log file)")

    if run_dir:
        analysis = run_dir / "analysis"
        print("\n--- TELEMETRY JSONL (tail) ---")
        for name in (
            "candidates.jsonl",
            "selected.jsonl",
            "rejected.jsonl",
            "rejections.jsonl",
            "strategy_decisions.jsonl",
            "shortlist.jsonl",
        ):
            path = analysis / name
            if path.is_file():
                line_count = sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
                print(f"  {name}: lines={line_count}")
                top = _analyze_jsonl(path, key="reason_code")
                if not top:
                    top = _analyze_jsonl(path, key="stage")
                if top:
                    for reason, cnt in top.most_common(8):
                        print(f"    {cnt:5d}  {reason}")

    print("\n--- SQLITE ---")
    print(json.dumps(_sqlite_summary(settings.db_path), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

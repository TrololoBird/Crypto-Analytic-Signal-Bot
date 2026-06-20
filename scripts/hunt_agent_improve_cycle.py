#!/usr/bin/env python3
"""Analyze one autonomous cycle report → prioritized improve_queue for agent follow-up.

Read-only telemetry; does not patch code. Called from hunt_autonomous_8h.py after each cycle.

  .venv/bin/python scripts/hunt_agent_improve_cycle.py \\
      --run-dir hunt/data/session/autonomous_<run_id> \\
      --cycle-report hunt/data/session/autonomous_<run_id>/cycle_01.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HUNT_DATA = ROOT / "hunt" / "data"
SIGNAL_EVENTS = HUNT_DATA / "signal_events.jsonl"

HOT_PATHS = frozenset({"hot_ws", "hot_delta", "hot_bootstrap", "hot_carry"})
ERROR_PATHS = frozenset({"hot_error", "rest_error"})


def _tail_jsonl(path: Path, *, max_lines: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines: list[str] = []
    with path.open("rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                lines.append(line)
    out: list[dict[str, Any]] = []
    for line in lines[-max_lines:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _cvd_missing_in_events(events: list[dict[str, Any]]) -> int:
    missing = 0
    for ev in events:
        if ev.get("stage") not in {"delivered", "telegram_sent", "signal_confirmed"}:
            continue
        payload = ev.get("payload") or ev.get("signal") or {}
        text = str(payload.get("text") or payload.get("body") or "")
        meta = payload.get("meta") or {}
        if "CVD недоступен" in text or meta.get("cvd_unavailable"):
            missing += 1
    return missing


def analyze_cycle_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ordered list of improvement items {id, severity, hint, evidence}."""
    items: list[dict[str, Any]] = []
    ticks = report.get("ticks") or {}
    paths: dict[str, int] = dict(ticks.get("tick_path") or {})
    total = max(int(ticks.get("lines") or 0), 1)
    unknown = int(paths.get("unknown", 0))
    hot_carry = int(ticks.get("hot_carry") or paths.get("hot_carry", 0))
    hot_total = sum(int(paths.get(p, 0)) for p in HOT_PATHS)
    errors = ticks.get("row_errors") or []

    if unknown > 0 and unknown / total >= 0.15:
        items.append(
            {
                "id": "tick_path_unknown",
                "severity": "high",
                "hint": "Ensure every tick row sets tick_path (incl. timeout/exception paths).",
                "evidence": {"unknown": unknown, "total": total, "ratio": round(unknown / total, 3)},
            }
        )

    if hot_total > 0 and hot_carry / hot_total < 0.25:
        items.append(
            {
                "id": "hot_carry_low",
                "severity": "medium",
                "hint": "Seed carry after hot_bootstrap/hot_ws so hot_carry reuses HTF context.",
                "evidence": {
                    "hot_carry": hot_carry,
                    "hot_total": hot_total,
                    "ratio": round(hot_carry / hot_total, 3),
                    "tick_path": paths,
                },
            }
        )

    err_paths = sum(int(paths.get(p, 0)) for p in ERROR_PATHS)
    if err_paths > 0:
        items.append(
            {
                "id": "tick_errors",
                "severity": "medium",
                "hint": "Investigate symbol_tick_timeout / IncompleteReadError on hot path.",
                "evidence": {"error_paths": err_paths, "samples": errors[:5]},
            }
        )

    needs_fix = report.get("needs_fix") or []
    if needs_fix:
        items.append(
            {
                "id": "static_gate_fail",
                "severity": "critical",
                "hint": "Fix failing compileall/check_logic/check_scenarios/budget before next cycle.",
                "evidence": {"needs_fix": needs_fix, "gates": report.get("gates")},
            }
        )

    confirmed = ticks.get("confirmed") or {}
    if total >= 50 and not confirmed.get("short") and not confirmed.get("long"):
        items.append(
            {
                "id": "zero_confirmed",
                "severity": "low",
                "hint": "No confirmed setups this cycle — review thresholds or universe filter.",
                "evidence": {"lines": total, "confirmed": confirmed},
            }
        )

    events = _tail_jsonl(SIGNAL_EVENTS)
    cvd_miss = _cvd_missing_in_events(events)
    if cvd_miss:
        items.append(
            {
                "id": "cvd_unavailable",
                "severity": "high",
                "hint": "session_cvd in tf_snapshot + _f_signed for negative CVD in deep_signal.",
                "evidence": {"recent_deliveries_with_cvd_missing": cvd_miss},
            }
        )

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    items.sort(key=lambda x: sev_order.get(str(x.get("severity")), 9))
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hunt autonomous cycle → improve queue")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cycle-report", type=Path, required=True)
    parser.add_argument("--cycle", type=int, default=0)
    args = parser.parse_args(argv)

    run_dir: Path = args.run_dir
    report_path: Path = args.cycle_report
    if not report_path.is_file():
        print(json.dumps({"error": f"missing report: {report_path}"}))
        return 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    items = analyze_cycle_report(report)
    queue_path = run_dir / "improve_queue.json"
    existing: list[dict[str, Any]] = []
    if queue_path.exists():
        try:
            existing = json.loads(queue_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []

    entry = {
        "cycle": args.cycle or report.get("cycle"),
        "at": datetime.now(UTC).isoformat(),
        "items": items,
        "tick_path": (report.get("ticks") or {}).get("tick_path"),
    }
    existing.append(entry)
    queue_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"queued": len(items), "path": str(queue_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

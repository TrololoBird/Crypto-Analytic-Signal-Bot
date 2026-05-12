from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.common import bootstrap_repo_path
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from common import bootstrap_repo_path


ROOT = bootstrap_repo_path()
DEFAULT_DB = ROOT / "data" / "bot" / "bot.db"
DEFAULT_REPORT = ROOT / "logs" / "21_outcome_r_recompute.md"


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _setup_quality(r_multiple: float) -> str:
    if r_multiple >= 1.0:
        return "good"
    if r_multiple <= -1.0:
        return "bad"
    return "neutral"


def _recomputed_row(row: sqlite3.Row) -> dict[str, Any] | None:
    entry_price = row["entry_price"]
    exit_price = row["exit_price"]
    initial_stop = row["initial_stop"]
    if entry_price is None or exit_price is None or initial_stop is None:
        return None

    entry = float(entry_price)
    exit_ = float(exit_price)
    stop = float(initial_stop)
    if entry <= 0.0:
        return None

    if row["direction"] == "short":
        pnl_pct = (entry - exit_) / entry * 100.0
        max_loss_pct = -1.0 * (stop - entry) / entry * 100.0
    else:
        pnl_pct = (exit_ - entry) / entry * 100.0
        max_loss_pct = (stop - entry) / entry * 100.0

    risk = abs(entry - stop)
    r_multiple = pnl_pct / (risk / entry * 100.0) if risk > 0 else 0.0
    return {
        "tracking_id": row["tracking_id"],
        "symbol": row["symbol"],
        "setup_id": row["setup_id"],
        "result": row["result"],
        "old_pnl_pct": float(row["pnl_pct"] or 0.0),
        "new_pnl_pct": pnl_pct,
        "old_r": float(row["pnl_r_multiple"] or 0.0),
        "new_r": r_multiple,
        "old_max_loss_pct": float(row["max_loss_pct"] or 0.0),
        "new_max_loss_pct": max_loss_pct,
        "setup_quality": _setup_quality(r_multiple),
    }


def recompute(db_path: Path, *, apply: bool) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    backup_path: Path | None = None
    if apply:
        backup_path = db_path.with_name(f"{db_path.name}.backup_r_recompute_{_now_stamp()}")
        shutil.copy2(db_path, backup_path)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT
                so.tracking_id,
                so.symbol,
                so.setup_id,
                so.direction,
                so.result,
                so.entry_price,
                so.exit_price,
                so.pnl_pct,
                so.pnl_r_multiple,
                so.max_loss_pct,
                a.initial_stop
            FROM signal_outcomes so
            LEFT JOIN active_signals a ON a.tracking_id = so.tracking_id
            ORDER BY COALESCE(so.closed_at, so.created_at) ASC
            """
        ).fetchall()

        recomputed = [_recomputed_row(row) for row in rows]
        usable = [row for row in recomputed if row is not None]
        changed = [
            row
            for row in usable
            if abs(row["old_r"] - row["new_r"]) > 0.0001
            or abs(row["old_pnl_pct"] - row["new_pnl_pct"]) > 0.0001
            or abs(row["old_max_loss_pct"] - row["new_max_loss_pct"]) > 0.0001
        ]

        if apply and changed:
            con.executemany(
                """
                UPDATE signal_outcomes
                SET
                    pnl_pct = ?,
                    pnl_r_multiple = ?,
                    max_loss_pct = ?,
                    mae = ?,
                    setup_quality = ?
                WHERE tracking_id = ?
                """,
                [
                    (
                        row["new_pnl_pct"],
                        row["new_r"],
                        row["new_max_loss_pct"],
                        row["new_max_loss_pct"],
                        row["setup_quality"],
                        row["tracking_id"],
                    )
                    for row in changed
                ],
            )
            con.commit()

        max_abs_r_delta = max(
            (abs(row["old_r"] - row["new_r"]) for row in changed), default=0.0
        )
        return {
            "db_path": str(db_path),
            "applied": apply,
            "backup_path": str(backup_path) if backup_path else None,
            "rows_scanned": len(rows),
            "rows_recomputable": len(usable),
            "rows_changed": len(changed),
            "max_abs_r_delta": max_abs_r_delta,
            "examples": sorted(
                changed,
                key=lambda row: abs(row["old_r"] - row["new_r"]),
                reverse=True,
            )[:20],
        }
    finally:
        con.close()


def write_report(summary: dict[str, Any], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Outcome R-Multiple Recompute",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## Evidence Boundary",
        "",
        "- Confirmed: recompute uses persisted `signal_outcomes` joined to `active_signals.initial_stop` by `tracking_id`.",
        "- Confirmed: `--apply` creates a timestamped SQLite backup before updating rows.",
        "- Inference: `initial_stop` is treated as the original planned risk anchor for R-multiple analytics.",
        "",
        "## Summary",
        "",
        f"- Applied: {summary['applied']}",
        f"- DB: {summary['db_path']}",
        f"- Backup: {summary['backup_path'] or 'not created'}",
        f"- Rows scanned: {summary['rows_scanned']}",
        f"- Rows recomputable: {summary['rows_recomputable']}",
        f"- Rows changed: {summary['rows_changed']}",
        f"- Max absolute R delta: {summary['max_abs_r_delta']:.4f}",
        "",
        "## Largest Changes",
        "",
        "| Tracking ID | Symbol | Setup | Result | Old R | New R | Old Max Loss % | New Max Loss % |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in summary["examples"]:
        lines.append(
            "| {tracking_id} | {symbol} | {setup_id} | {result} | {old_r:.4f} | {new_r:.4f} | {old_max_loss_pct:.4f} | {new_max_loss_pct:.4f} |".format(
                **row
            )
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recompute historical signal_outcomes R multiples from initial stops."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    summary = recompute(args.db, apply=args.apply)
    write_report(summary, args.report)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

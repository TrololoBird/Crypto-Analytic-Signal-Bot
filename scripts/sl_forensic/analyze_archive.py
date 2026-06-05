#!/usr/bin/env python3
"""Analyze persistent forensic archive with small-N tier framework.

Usage:
    python scripts/sl_forensic/analyze_archive.py
    python scripts/sl_forensic/analyze_archive.py --no-report
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import aiosqlite

try:
    import _bootstrap
except ModuleNotFoundError:  # pragma: no cover
    from scripts.sl_forensic import _bootstrap  # noqa: F401

from scripts.sl_forensic._archive_migrations import migrate_forensic_archive
from scripts.sl_forensic._paths import (
    FORENSIC_ARCHIVE_PATH,
    REPORT_ARCHIVE_PATH,
    ensure_forensics_dir,
)

SL_LIKE = (
    "stop_loss",
    "breakeven_stop",
    "trailing_stop",
)


def _is_sl_result(result: str | None) -> bool:
    r = str(result or "").lower()
    return any(token in r for token in SL_LIKE)


async def _scalar(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    async with conn.execute(sql, params) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def analyze_archive(*, write_report: bool = True) -> str:
    ensure_forensics_dir()
    lines: list[str] = ["# Forensic Archive Analysis", ""]

    if not FORENSIC_ARCHIVE_PATH.exists():
        msg = "forensic_archive.db not found — run export_to_archive.py first"
        print(msg)
        lines.extend([msg, ""])
        if write_report:
            REPORT_ARCHIVE_PATH.write_text("\n".join(lines), encoding="utf-8")
        return "\n".join(lines)

    async with aiosqlite.connect(FORENSIC_ARCHIVE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await migrate_forensic_archive(conn)

        print("=== FORENSIC ARCHIVE RUN HISTORY ===")
        lines.extend(["## Run history", ""])
        async with conn.execute(
            """
            SELECT run_id, run_date, total_signals, sl_count, tp_count, codebase_hash, notes
            FROM forensic_runs
            ORDER BY run_date DESC
            LIMIT 20
            """
        ) as cursor:
            runs = await cursor.fetchall()
        if runs:
            lines.append("| run_date | exported | SL | TP | hash | notes |")
            lines.append("|----------|---------:|---:|---:|------|-------|")
            for run in runs:
                print(
                    f"  {run['run_date'][:19]} | n={run['total_signals']} "
                    f"sl={run['sl_count']} | {run['codebase_hash'] or '-'}"
                )
                note = (run["notes"] or "")[:40]
                lines.append(
                    f"| {run['run_date'][:19]} | {run['total_signals']} | "
                    f"{run['sl_count']} | {run['tp_count']} | "
                    f"{run['codebase_hash'] or '-'} | {note} |"
                )
        else:
            print("  (no runs yet)")
            lines.append("_No export runs recorded yet._")
        lines.append("")

        total_sl = int(
            await _scalar(
                conn,
                """
                SELECT COUNT(*) FROM forensic_cases
                WHERE result IN ('stop_loss','breakeven_stop','trailing_stop')
                """,
            )
            or 0
        )
        total_all = int(await _scalar(conn, "SELECT COUNT(*) FROM forensic_cases") or 0)

        print(f"\nArchive: {total_all} total cases, {total_sl} SL-like")
        lines.extend([f"**Archive totals:** {total_all} cases, {total_sl} SL", ""])

        # TIER 1
        print("\n=== TIER 1: DETERMINISTIC BUGS (fix immediately) ===")
        lines.extend(["## TIER 1 — Deterministic (n=1)", ""])

        async with conn.execute(
            """
            SELECT setup_id, symbol, forensic_id, run_date, sl_verdict, sl_type, sl_subtype
            FROM forensic_cases
            WHERE false_signal_recheck = 0
              AND sl_subtype = 'FALSE_SIGNAL'
            ORDER BY setup_id, run_date
            """
        ) as cursor:
            false_signals = await cursor.fetchall()

        if false_signals:
            print("FALSE_SIGNAL DETECTED:")
            lines.append("### D1 — FALSE_SIGNAL (recheck failed on confirmed data)")
            for c in false_signals:
                print(f"  {c['setup_id']} | {c['symbol']} | run {c['run_date'][:10]}")
                lines.append(
                    f"- `{c['setup_id']}` **{c['symbol']}** "
                    f"(run {c['run_date'][:10]}) — {c['sl_verdict'] or ''}"
                )
            print("  → ACTION REQUIRED: apply df[-2] fix to listed strategies")
            lines.append("")
            lines.append("**Action:** apply confirmed-bar / df[-2] fix to listed strategies.")
        else:
            print("FALSE_SIGNAL: none detected ✓")
            lines.append("### D1 — FALSE_SIGNAL: none detected ✓")

        confirmed_zero = int(
            await _scalar(
                conn,
                """
                SELECT COUNT(*) FROM forensic_cases
                WHERE confirmed_candle = 0
                  AND result IN ('stop_loss','breakeven_stop','trailing_stop')
                """,
            )
            or 0
        )
        confirmed_null = int(
            await _scalar(
                conn,
                """
                SELECT COUNT(*) FROM forensic_cases
                WHERE confirmed_candle IS NULL
                  AND result IN ('stop_loss','breakeven_stop','trailing_stop')
                """,
            )
            or 0
        )
        pct = confirmed_zero / total_sl * 100 if total_sl else 0.0
        lines.append("")
        lines.append("### D2 — confirmed_candle tracking")
        if pct > 90 and total_sl >= 3:
            print(f"TRACKING BUG: {pct:.0f}% of SL cases have confirmed_candle=0")
            print("  → Audit how confirmed_candle field is populated in _confirmed_candle.py")
            lines.append(
                f"**TRACKING BUG:** {confirmed_zero}/{total_sl} ({pct:.0f}%) "
                f"have `confirmed_candle=0`; {confirmed_null} unknown."
            )
            lines.append("")
            lines.append(
                "Audit `_confirmed_candle.infer_confirmed_candle()` and features JSON population."
            )
        else:
            print(f"confirmed_candle=0: {confirmed_zero}/{total_sl} ({pct:.0f}%) — within range")
            lines.append(
                f"`confirmed_candle=0`: {confirmed_zero}/{total_sl} ({pct:.0f}%), "
                f"unknown: {confirmed_null} — within range or insufficient n."
            )

        async with conn.execute(
            """
            SELECT setup_id, symbol, time_to_sl_min, entry_deviation_atr, mfe, run_date
            FROM forensic_cases
            WHERE time_to_sl_min < 5
              AND result IN ('stop_loss','breakeven_stop','trailing_stop')
              AND (mfe IS NULL OR mfe <= 0.05)
            ORDER BY time_to_sl_min
            """
        ) as cursor:
            fast_sl = await cursor.fetchall()

        lines.append("")
        lines.append("### D3 — Ultra-fast SL (<5 min, zero MFE)")
        if fast_sl:
            print(f"\nULTRA-FAST SL (<5 min): {len(fast_sl)} cases")
            for c in fast_sl:
                print(f"  {c['setup_id']} {c['symbol']} {c['time_to_sl_min']}min")
                lines.append(
                    f"- `{c['setup_id']}` {c['symbol']} — "
                    f"{c['time_to_sl_min']} min, MFE={c['mfe'] or 0:.2f}"
                )
            lines.append("")
            lines.append("Check entry_staleness filter was active for these cases.")
        else:
            print("\nULTRA-FAST SL: none")
            lines.append("_None detected._")

        # TIER 2
        print("\n=== TIER 2: CASE REVIEW PATTERNS ===")
        lines.extend(["", "## TIER 2 — Case review (n=3–10)", ""])

        async with conn.execute(
            """
            SELECT setup_id, sl_type, sl_subtype, COUNT(*) AS n
            FROM forensic_cases
            WHERE result IN ('stop_loss','breakeven_stop','trailing_stop')
              AND sl_type IS NOT NULL
            GROUP BY setup_id, sl_type, sl_subtype
            HAVING n >= 2
            ORDER BY n DESC
            """
        ) as cursor:
            patterns = await cursor.fetchall()

        if patterns:
            for p in patterns:
                label = f"{p['sl_type']}/{p['sl_subtype']}"
                print(f"  {p['setup_id']}: {p['n']}× {label}")
                lines.append(f"- **{p['setup_id']}:** {p['n']}× {label}")
                if p["sl_type"] == "TIMING_OFF" and p["n"] >= 2:
                    print(f"    → CASE REVIEW: widen ATR for {p['setup_id']}")
                    lines.append(f"  - → widen ATR multiplier for `{p['setup_id']}`")
                if p["sl_type"] == "STOP_HUNT" and p["n"] >= 2:
                    print(f"    → CASE REVIEW: post-wick entry or widen SL for {p['setup_id']}")
                    lines.append(f"  - → post-wick entry delay or widen SL for `{p['setup_id']}`")
        else:
            print("  (no repeated patterns yet — need 2+ cases per setup/type)")
            lines.append("_No repeated patterns yet (need 2+ cases per setup/type)._")

        async with conn.execute(
            """
            SELECT run_date, setup_id, symbol, btc_move_sl_candle_pct
            FROM forensic_cases
            WHERE btc_caused_sl = 1
            ORDER BY run_date
            """
        ) as cursor:
            btc_drag = await cursor.fetchall()
        if len(btc_drag) >= 2:
            lines.append("")
            lines.append("### C3 — BTC_DRAG sessions")
            lines.extend(
                (
                    f"- {c['run_date'][:10]} {c['setup_id']} {c['symbol']} "
                    f"BTC {c['btc_move_sl_candle_pct']:.2f}%"
                )
                for c in btc_drag
            )

        # TIER 3
        print("\n=== TIER 3: STATISTICAL HYPOTHESES (deferred) ===")
        lines.extend(["", "## TIER 3 — Statistical (deferred)", ""])
        targets = [
            ("Fix B: adaptive ATR", 30, "ATR band SL analysis"),
            ("Fix C: regime filter", 50, "direction vs regime"),
            ("Fix D: score floor", 30, "score quartile breakdown"),
        ]
        for name, required, desc in targets:
            progress = min(total_sl / required * 100, 100) if required else 0
            bar = "█" * int(progress / 10) + "░" * (10 - int(progress / 10))
            status = "READY TO ANALYZE" if total_sl >= required else "ACCUMULATING DATA"
            print(f"  {name}")
            print(f"    [{bar}] {total_sl}/{required} cases ({progress:.0f}%)")
            print(f"    Status: {status}")
            lines.append(f"### {name}")
            lines.append(f"- Progress: `[{bar}]` {total_sl}/{required} ({progress:.0f}%)")
            lines.append(f"- Status: **{status}** — {desc}")
            lines.append("")

    report = "\n".join(lines)
    if write_report:
        REPORT_ARCHIVE_PATH.write_text(report, encoding="utf-8")
        print(f"\nReport: {REPORT_ARCHIVE_PATH}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze forensic archive (small-N tiers)")
    parser.add_argument("--no-report", action="store_true", help="Skip writing markdown report")
    args = parser.parse_args()
    asyncio.run(analyze_archive(write_report=not args.no_report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

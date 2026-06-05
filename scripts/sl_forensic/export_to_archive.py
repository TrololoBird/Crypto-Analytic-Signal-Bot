#!/usr/bin/env python3
"""Export bot.db outcomes to persistent forensic archive (run BEFORE bot.db wipe).

Usage:
    python scripts/sl_forensic/export_to_archive.py
    python scripts/sl_forensic/export_to_archive.py --run-notes "smoke test #3 post fix-sl-A"
    python scripts/sl_forensic/export_to_archive.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.domain.config import load_settings

from scripts.sl_forensic._archive_migrations import migrate_forensic_archive
from scripts.sl_forensic._case_builder import EXPORT_QUERY, enrich_sl_case, row_to_base_case
from scripts.sl_forensic._fetcher import CandleFetcher
from scripts.sl_forensic._paths import (
    FORENSIC_ARCHIVE_PATH,
    SL_RESULTS,
    ensure_forensics_dir,
    git_short_hash,
)

LOG = logging.getLogger("sl_forensic.export")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )


async def _existing_tracking_ids(conn: aiosqlite.Connection) -> set[str]:
    async with conn.execute(
        "SELECT tracking_id FROM forensic_cases WHERE tracking_id IS NOT NULL"
    ) as cursor:
        rows = await cursor.fetchall()
    return {str(r[0]) for r in rows if r[0]}


async def _active_fixes(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    async with conn.execute(
        """
        SELECT fix_id, fix_name, commit_hash, target_setup_ids, hypothesis
        FROM fix_history
        WHERE status = 'ACTIVE'
        ORDER BY applied_at
        """
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "fix_id": r[0],
            "fix_name": r[1],
            "commit_hash": r[2],
            "target_setup_ids": json.loads(r[3] or "[]"),
            "hypothesis": r[4],
        }
        for r in rows
    ]


async def _insert_case(
    conn: aiosqlite.Connection,
    case: dict[str, Any],
    *,
    run_id: str,
    run_date: str,
    codebase_hash: str | None,
    fixes_applied: list[str],
) -> None:
    await conn.execute(
        """
        INSERT INTO forensic_cases (
            forensic_id, run_id, run_date, bot_version,
            signal_id, tracking_id, setup_id, symbol, direction, timeframe,
            result, pnl_pct,
            signal_created_at, entry_activated_at, sl_hit_at,
            time_to_entry_min, time_to_sl_min,
            entry_price, sl_price, tp1_price, rr_ratio, sl_distance_pct,
            score, atr_pct, spread_bps,
            sl_type, sl_subtype, sl_verdict,
            post_sl_tp1_reached, post_sl_tp1_candles,
            post_sl_max_recovery, post_sl_max_adverse,
            btc_move_sl_candle_pct, btc_direction_match, btc_caused_sl,
            confirmed_candle, entry_deviation_atr, false_signal_recheck,
            market_regime, btc_bias, direction_vs_bias,
            fixes_applied, codebase_hash, indicator_snapshot,
            mfe, mae, analyzed_at
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, datetime('now')
        )
        """,
        (
            case.get("forensic_id") or str(uuid.uuid4()),
            run_id,
            run_date,
            codebase_hash,
            case.get("signal_id"),
            case.get("tracking_id"),
            case["setup_id"],
            case["symbol"],
            case["direction"],
            case.get("timeframe") or "15m",
            case["result"],
            case.get("pnl_pct"),
            case.get("signal_created_at"),
            case.get("entry_activated_at"),
            case.get("sl_hit_at"),
            case.get("time_to_entry_min"),
            case.get("time_to_sl_min"),
            case.get("entry_price"),
            case.get("sl_price"),
            case.get("tp1_price"),
            case.get("rr_ratio"),
            case.get("sl_distance_pct"),
            case.get("score"),
            case.get("atr_pct"),
            case.get("spread_bps"),
            case.get("sl_type"),
            case.get("sl_subtype"),
            case.get("sl_verdict"),
            1 if case.get("post_sl_tp1_reached") else 0 if case.get("post_sl_tp1_reached") is not None else None,
            case.get("post_sl_tp1_candles"),
            case.get("post_sl_max_recovery"),
            case.get("post_sl_max_adverse"),
            case.get("btc_move_sl_candle_pct"),
            case.get("btc_direction_match"),
            1 if case.get("btc_caused_sl") else 0 if case.get("btc_caused_sl") is not None else None,
            case.get("confirmed_candle"),
            case.get("entry_deviation_atr"),
            case.get("false_signal_recheck"),
            case.get("market_regime"),
            case.get("btc_bias"),
            case.get("direction_vs_bias"),
            json.dumps(fixes_applied),
            codebase_hash,
            json.dumps(case.get("indicator_snapshot") or {}),
            case.get("mfe"),
            case.get("mae"),
        ),
    )


async def export_to_archive(*, notes: str = "", dry_run: bool = False) -> dict[str, Any]:
    settings = load_settings()
    bot_db = Path(getattr(settings, "db_path", ROOT / "data/bot/bot.db"))
    ensure_forensics_dir()
    codebase_hash = git_short_hash()
    run_id = str(uuid.uuid4())
    run_date = datetime.now(UTC).isoformat()
    fix_ids: list[str] = []

    if not bot_db.exists():
        print(f"bot.db not found: {bot_db}")
        return {"exported": 0, "skipped": 0, "run_id": run_id}

    async with aiosqlite.connect(f"file:{bot_db}?mode=ro", uri=True) as bot_conn:
        bot_conn.row_factory = aiosqlite.Row
        async with bot_conn.execute(EXPORT_QUERY) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        print("No closed outcomes in bot.db")
        return {"exported": 0, "skipped": 0, "run_id": run_id}

    if dry_run:
        async with aiosqlite.connect(FORENSIC_ARCHIVE_PATH) as arch_conn:
            await migrate_forensic_archive(arch_conn)
            existing = await _existing_tracking_ids(arch_conn)
        new_rows = [r for r in rows if str(r["tracking_id"]) not in existing]
        print(f"DRY RUN: would export {len(new_rows)} new / skip {len(rows) - len(new_rows)} existing")
        print(f"Run ID: {run_id}")
        print(f"Codebase: {codebase_hash or 'unknown'}")
        return {"exported": len(new_rows), "skipped": len(rows) - len(new_rows), "run_id": run_id}

    exported = 0
    skipped = 0
    sl_count = tp_count = expired_count = 0

    async with aiosqlite.connect(FORENSIC_ARCHIVE_PATH) as arch_conn:
        await migrate_forensic_archive(arch_conn)
        existing = await _existing_tracking_ids(arch_conn)
        fixes = await _active_fixes(arch_conn)
        fix_ids = [str(f["fix_id"]) for f in fixes]

        fetcher = CandleFetcher()
        try:
            for row in rows:
                tracking_id = str(row["tracking_id"])
                if tracking_id in existing:
                    skipped += 1
                    continue

                case = row_to_base_case(row)
                case["forensic_id"] = str(uuid.uuid4())
                result = str(case.get("result") or "")

                if result in SL_RESULTS:
                    sl_count += 1
                    try:
                        case = await enrich_sl_case(
                            case,
                            settings=settings,
                            fetcher=fetcher,
                            do_recheck=True,
                        )
                    except Exception:
                        LOG.warning(
                            "SL enrich failed | tracking_id=%s — storing DB fields only",
                            tracking_id,
                            exc_info=True,
                        )
                elif result in {"tp1_hit", "tp2_hit", "tp3_hit"}:
                    tp_count += 1
                elif "expired" in result:
                    expired_count += 1

                from scripts.sl_forensic._confirmed_candle import infer_confirmed_candle

                if case.get("confirmed_candle") is None:
                    case["confirmed_candle"] = infer_confirmed_candle(
                        setup_id=str(case["setup_id"]),
                        signal_created_at=case.get("signal_created_at"),
                        features_snapshot=case.get("indicator_snapshot"),
                    )

                await _insert_case(
                    arch_conn,
                    case,
                    run_id=run_id,
                    run_date=run_date,
                    codebase_hash=codebase_hash,
                    fixes_applied=fix_ids,
                )
                exported += 1
        finally:
            await fetcher.close()

        await arch_conn.execute(
            """
            INSERT INTO forensic_runs (
                run_id, run_date, codebase_hash,
                total_signals, sl_count, tp_count, expired_count,
                fixes_active, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                run_date,
                codebase_hash,
                exported,
                sl_count,
                tp_count,
                expired_count,
                json.dumps(fix_ids),
                notes or None,
            ),
        )
        await arch_conn.commit()

    print(f"Exported: {exported} new cases ({skipped} already existed, skipped)")
    print(f"Run ID: {run_id}")
    print(f"Codebase: {codebase_hash or 'unknown'}")
    print(f"Active fixes: {fix_ids or '[]'}")
    return {
        "exported": exported,
        "skipped": skipped,
        "run_id": run_id,
        "codebase_hash": codebase_hash,
        "fixes_active": fix_ids,
    }


def main() -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description="Export bot.db outcomes to forensic archive")
    parser.add_argument("--run-notes", default="", help="Notes for this export batch")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    asyncio.run(export_to_archive(notes=args.run_notes, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""SL Forensic Engine — replay candles, classify SL hits, persist + report.

Usage:
  python scripts/sl_forensic_engine.py
  python scripts/sl_forensic_engine.py --tracking-id <id>
  python scripts/sl_forensic_engine.py --bars-before 60 --bars-after 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

try:
    from scripts.common import bootstrap_repo_path, configure_script_logging
except ModuleNotFoundError:  # pragma: no cover
    from common import bootstrap_repo_path, configure_script_logging

import aiosqlite

from bot.domain.config import load_settings
from bot.market.kline_window import fetch_forensic_candle_pack
from bot.market.rest_impl import BinanceClientImpl
from bot.migrations import migrate_db
from bot.persistence.sl_forensics import (
    SL_RESULTS,
    ForensicCase,
    build_forensic_case,
    render_aggregate_report,
    render_case_card,
)

LOG = configure_script_logging("scripts.sl_forensic_engine")


def _load_sl_rows(
    db_path: Path,
    *,
    tracking_id: str | None = None,
    since_hours: int | None = None,
) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    query = f"""
        SELECT
            so.tracking_id,
            so.symbol,
            so.setup_id,
            so.direction,
            so.timeframe,
            so.result,
            so.created_at,
            so.closed_at,
            so.entry_price,
            so.exit_price,
            so.mfe,
            so.mae,
            so.time_to_entry_min,
            so.time_to_exit_min,
            so.features,
            so.pnl_pct,
            so.pnl_r_multiple,
            a.score,
            a.atr_pct,
            a.bias_4h,
            a.entry_mid,
            a.activation_price,
            a.stop,
            a.take_profit_1,
            a.activated_at
        FROM signal_outcomes so
        JOIN active_signals a ON so.tracking_id = a.tracking_id
        WHERE so.result IN ({",".join("?" * len(SL_RESULTS))})
    """
    params: list[object] = list(SL_RESULTS)
    if tracking_id:
        query += " AND so.tracking_id = ?"
        params.append(tracking_id)
    if since_hours is not None:
        query += " AND so.created_at > datetime('now', ?)"
        params.append(f"-{since_hours} hours")
    query += " ORDER BY so.closed_at DESC"
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    return rows


def _upsert_forensic(db_path: Path, case: ForensicCase, card: str) -> None:
    row = case.to_row_dict()
    row["card_markdown"] = card
    row["analyzed_at"] = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO sl_forensics (
            tracking_id, symbol, setup_id, direction, timeframe,
            forensic_type, forensic_subtype, label, sl_root_cause_legacy,
            mfe, mae, post_sl_favorable_pct, post_sl_tp1_reached,
            closed_candle_valid, entry_deviation_pct, btc_correlation_at_sl,
            active_minutes, score, atr_pct, recommendations, metrics,
            card_markdown, analyzed_at, signal_created_at, sl_closed_at
        ) VALUES (
            :tracking_id, :symbol, :setup_id, :direction, :timeframe,
            :forensic_type, :forensic_subtype, :label, :sl_root_cause_legacy,
            :mfe, :mae, :post_sl_favorable_pct, :post_sl_tp1_reached,
            :closed_candle_valid, :entry_deviation_pct, :btc_correlation_at_sl,
            :active_minutes, :score, :atr_pct, :recommendations, :metrics,
            :card_markdown, :analyzed_at, :signal_created_at, :sl_closed_at
        )
        ON CONFLICT(tracking_id) DO UPDATE SET
            forensic_type = excluded.forensic_type,
            forensic_subtype = excluded.forensic_subtype,
            label = excluded.label,
            mfe = excluded.mfe,
            mae = excluded.mae,
            post_sl_favorable_pct = excluded.post_sl_favorable_pct,
            post_sl_tp1_reached = excluded.post_sl_tp1_reached,
            closed_candle_valid = excluded.closed_candle_valid,
            entry_deviation_pct = excluded.entry_deviation_pct,
            btc_correlation_at_sl = excluded.btc_correlation_at_sl,
            recommendations = excluded.recommendations,
            metrics = excluded.metrics,
            card_markdown = excluded.card_markdown,
            analyzed_at = excluded.analyzed_at
        """,
        row,
    )
    conn.commit()
    conn.close()


async def _migrate(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await migrate_db(conn)


async def _analyze_rows(
    rows: list[dict],
    *,
    client: BinanceClientImpl,
    db_path: Path,
    bars_before: int,
    bars_after: int,
    out_dir: Path,
) -> list[ForensicCase]:
    cases: list[ForensicCase] = []
    cards_dir = out_dir / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    for row in rows:
        tid = str(row.get("tracking_id") or "")
        symbol = str(row.get("symbol") or "")
        tf = str(row.get("timeframe") or "15m").split("+")[0]
        event_raw = row.get("activated_at") or row.get("closed_at") or row.get("created_at")
        try:
            event_dt = datetime.fromisoformat(str(event_raw))
        except (TypeError, ValueError):
            LOG.warning("skip %s: bad event time %s", tid, event_raw)
            continue
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=UTC)

        LOG.info("forensic %s %s %s @ %s", tid, symbol, row.get("setup_id"), event_dt.isoformat())
        try:
            packs = await fetch_forensic_candle_pack(
                client,
                symbol=symbol,
                signal_timeframe=tf,
                event_dt=event_dt,
                bars_before=bars_before,
                bars_after=bars_after,
            )
        except Exception as exc:
            LOG.warning("candle fetch failed %s: %s", symbol, exc)
            packs = {}

        tf_frame = packs.get(tf)
        if tf_frame is None or tf_frame.is_empty():
            tf_frame = packs.get("15m")
        btc_frame = packs.get("btc_15m")
        case = build_forensic_case(
            row,
            candles_15m=tf_frame,
            candles_1h=packs.get("1h"),
            btc_candles_15m=btc_frame,
            bars_before=bars_before,
            bars_after=bars_after,
        )
        card = render_case_card(case)
        cases.append(case)
        _upsert_forensic(db_path, case, card)
        (cards_dir / f"{tid}.md").write_text(card, encoding="utf-8")

    return cases


async def _async_main(args: argparse.Namespace) -> int:
    bootstrap_repo_path()
    settings = load_settings()
    db_path = Path(settings.db_path)
    await _migrate(db_path)

    rows = _load_sl_rows(
        db_path,
        tracking_id=args.tracking_id,
        since_hours=args.since_hours,
    )
    if not rows:
        LOG.info("no SL outcomes to analyze")
        return 0

    client = BinanceClientImpl(
        rest_timeout_seconds=float(settings.ws.rest_timeout_seconds),
        futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
        proxy_url=settings.network.proxy_url,
        trust_env=settings.network.trust_env,
    )
    try:
        out_dir = Path(args.output_dir)
        cases = await _analyze_rows(
            rows,
            client=client,
            db_path=db_path,
            bars_before=args.bars_before,
            bars_after=args.bars_after,
            out_dir=out_dir,
        )
    finally:
        await client.close()

    analyzed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    report = render_aggregate_report(cases, analyzed_at=analyzed_at)
    report_path = out_dir / "SL_FORENSIC_REPORT.md"
    report_path.write_text(report, encoding="utf-8")

    summary = {
        "cases": len(cases),
        "types": {},
        "report": str(report_path),
        "cards_dir": str(out_dir / "cards"),
    }
    from collections import Counter

    for ftype, n in Counter(c.forensic_type for c in cases).items():
        summary["types"][ftype] = n
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="SL forensic engine")
    parser.add_argument("--tracking-id", default=None)
    parser.add_argument("--since-hours", type=int, default=None)
    parser.add_argument("--bars-before", type=int, default=60)
    parser.add_argument("--bars-after", type=int, default=60)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/forensics"),
        help="Report + per-case cards output directory",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()

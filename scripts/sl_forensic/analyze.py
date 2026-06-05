#!/usr/bin/env python3
"""SL Forensic Analyzer — standalone post-mortem for all stop-loss hits.

Usage:
    python scripts/sl_forensic/analyze.py
    python scripts/sl_forensic/analyze.py --days 7
    python scripts/sl_forensic/analyze.py --signal-id <id>
    python scripts/sl_forensic/analyze.py --recheck
    python scripts/sl_forensic/analyze.py --report-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

try:
    import _bootstrap
except ModuleNotFoundError:  # pragma: no cover
    from scripts.sl_forensic import _bootstrap  # noqa: F401

from bot.domain.config import load_settings
from bot.migrations import migrate_db
from scripts.sl_forensic._classifier import (
    assess_closed_candle_validity,
    classify_sl,
    compute_btc_correlation,
    compute_entry_deviation,
    compute_post_sl_action,
    direction_vs_bias,
    extract_indicator_snapshot,
)
from scripts.sl_forensic._confirmed_candle import infer_confirmed_candle
from scripts.sl_forensic._fetcher import CandleFetcher
from scripts.sl_forensic._paths import ROOT
from scripts.sl_forensic._reporter import generate_aggregate_report
from scripts.sl_forensic._strategy_recheck import recheck_strategy, ts_ms_from_iso

LOG = logging.getLogger("sl_forensic.analyze")

SL_QUERY = """
    SELECT
        so.tracking_id,
        so.signal_id,
        so.setup_id,
        so.symbol,
        so.direction,
        so.timeframe,
        so.result,
        so.created_at AS signal_created_at,
        so.activated_at AS entry_activated_at,
        so.closed_at AS sl_hit_at,
        so.time_to_entry_min,
        so.time_to_exit_min,
        so.entry_price,
        so.exit_price AS sl_price,
        so.mfe,
        so.mae,
        so.pnl_pct,
        so.features,
        as2.score,
        as2.take_profit_1 AS tp1_price,
        as2.take_profit_2 AS tp2_price,
        as2.initial_stop,
        as2.entry_mid,
        as2.activation_price,
        as2.atr_pct,
        as2.spread_bps,
        as2.bias_4h,
        as2.tp1_hit_at,
        mc.market_regime,
        mc.btc_bias
    FROM signal_outcomes so
    JOIN active_signals as2 ON so.tracking_id = as2.tracking_id
    LEFT JOIN market_context mc ON mc.id = 1
    WHERE so.result IN ('stop_loss', 'breakeven_stop', 'trailing_stop')
      AND so.closed_at > datetime('now', '-{days} days')
      {signal_filter}
    ORDER BY so.closed_at DESC
"""


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SL forensic analyzer")
    parser.add_argument("--days", type=int, default=30, help="Lookback days for SL cases")
    parser.add_argument("--signal-id", type=str, default="", help="Analyze one signal_id")
    parser.add_argument("--recheck", action="store_true", help="Re-run strategy detectors")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Skip fetch; use cached sl_forensics rows",
    )
    return parser.parse_args()


def _sl_distance_pct(entry: float | None, sl: float | None) -> float:
    if not entry or not sl or entry <= 0:
        return 0.0
    return abs(entry - sl) / entry * 100.0


def _rr_ratio(entry: float | None, sl: float | None, tp1: float | None) -> float | None:
    if not entry or not sl or not tp1 or entry <= 0:
        return None
    risk = abs(entry - sl)
    reward = abs(tp1 - entry)
    if risk <= 0:
        return None
    return reward / risk


def _candles_json(candles: list[dict[str, Any]] | None) -> str:
    if not candles:
        return "[]"
    slim = [
        {
            "ts": c["ts"],
            "o": c["open"],
            "h": c["high"],
            "l": c["low"],
            "c": c["close"],
            "v": c["volume"],
        }
        for c in candles
    ]
    return json.dumps(slim)


async def _fetch_sl_rows(
    conn: aiosqlite.Connection,
    *,
    days: int,
    signal_id: str,
) -> list[aiosqlite.Row]:
    signal_filter = ""
    params: list[Any] = []
    if signal_id:
        signal_filter = "AND so.signal_id = ?"
        params.append(signal_id)
    query = SL_QUERY.format(days=days, signal_filter=signal_filter)
    conn.row_factory = aiosqlite.Row
    async with conn.execute(query, params) as cursor:
        return await cursor.fetchall()


async def _existing_forensic(
    conn: aiosqlite.Connection,
    tracking_id: str,
) -> dict[str, Any] | None:
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM sl_forensics WHERE tracking_id = ? ORDER BY analyzed_at DESC LIMIT 1",
        (tracking_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _load_cached_cases(conn: aiosqlite.Connection, days: int) -> list[dict[str, Any]]:
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        """
        SELECT sf.* FROM sl_forensics sf
        INNER JOIN (
            SELECT tracking_id, MAX(analyzed_at) AS latest
            FROM sl_forensics
            GROUP BY tracking_id
        ) dedup ON sf.tracking_id = dedup.tracking_id AND sf.analyzed_at = dedup.latest
        WHERE sf.sl_hit_at > datetime('now', ?)
        ORDER BY sf.sl_hit_at DESC
        """,
        (f"-{days} days",),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


def _row_to_base_case(row: aiosqlite.Row) -> dict[str, Any]:
    time_to_exit = int(row["time_to_exit_min"] or 0)
    time_to_entry = int(row["time_to_entry_min"] or 0)
    entry = float(row["entry_price"] or 0)
    initial_stop = float(row["initial_stop"] or 0)
    exit_price = float(row["sl_price"] or 0)
    result = str(row["result"] or "")
    # Breakeven/trailing exits report exit=entry; use planned stop for forensic distance.
    if result in {"breakeven_stop", "trailing_stop"} and initial_stop > 0:
        sl = initial_stop
    else:
        sl = exit_price or initial_stop
    tp1 = float(row["tp1_price"] or 0)
    tp2 = float(row["tp2_price"] or 0)
    features = row["features"]
    snapshot = extract_indicator_snapshot(features)
    funding = snapshot.get("funding_rate")
    entry_mid = float(row["entry_mid"] or entry)
    activation = float(row["activation_price"] or entry)
    entry_deviation_pct = abs(activation - entry_mid) / entry_mid * 100.0 if entry_mid > 0 else 0.0
    return {
        "tracking_id": row["tracking_id"],
        "signal_id": row["signal_id"],
        "setup_id": row["setup_id"],
        "symbol": row["symbol"],
        "direction": row["direction"],
        "timeframe": row["timeframe"] or "15m",
        "result": result,
        "signal_created_at": row["signal_created_at"],
        "entry_activated_at": row["entry_activated_at"],
        "sl_hit_at": row["sl_hit_at"],
        "tp1_hit_at": row["tp1_hit_at"],
        "time_to_entry_min": time_to_entry,
        "time_to_sl_min": max(0, time_to_exit - time_to_entry),
        "entry_price": entry,
        "sl_price": sl,
        "tp1_price": tp1,
        "tp2_price": tp2,
        "sl_distance_pct": _sl_distance_pct(entry, sl),
        "rr_ratio": _rr_ratio(entry, sl, tp1),
        "mfe": float(row["mfe"] or 0),
        "mae": float(row["mae"] or 0),
        "score": float(row["score"] or 0),
        "atr_pct": float(row["atr_pct"] or snapshot.get("atr_pct") or 0),
        "spread_bps": float(row["spread_bps"] or snapshot.get("spread_bps") or 0),
        "funding_rate": float(funding) if funding is not None else None,
        "bias_4h": row["bias_4h"] or snapshot.get("bias_4h"),
        "market_regime": row["market_regime"] or "unknown",
        "btc_bias": row["btc_bias"] or row["bias_4h"] or "neutral",
        "direction_vs_bias": direction_vs_bias(
            str(row["direction"]),
            row["btc_bias"] or row["bias_4h"],
        ),
        "entry_deviation_pct": entry_deviation_pct,
        "features": features,
        "indicator_snapshot": snapshot,
    }


async def _insert_forensic(conn: aiosqlite.Connection, case: dict[str, Any]) -> None:
    await conn.execute(
        "DELETE FROM sl_forensics WHERE tracking_id = ?",
        (case["tracking_id"],),
    )
    await conn.execute(
        """
        INSERT OR REPLACE INTO sl_forensics (
            forensic_id, signal_id, tracking_id, setup_id, symbol, direction, timeframe,
            signal_created_at, entry_activated_at, sl_hit_at, time_to_entry_min, time_to_sl_min,
            entry_price, sl_price, tp1_price, tp2_price, sl_distance_pct, rr_ratio,
            post_sl_candles_analyzed, post_sl_max_adverse_pct, post_sl_max_recovery_pct,
            post_sl_tp1_reached, post_sl_tp1_candles, post_sl_price_at_close,
            btc_move_in_sl_candle_pct, btc_direction_match, btc_caused_sl,
            score, atr_pct, spread_bps, funding_rate, entry_deviation_atr_mult,
            entry_candle_was_confirmed, market_regime, btc_bias, direction_vs_bias,
            sl_type, sl_subtype, sl_verdict,
            candles_signal_tf, candles_1h, candles_4h, candles_btc_signal,
            strategy_recheck_valid, indicator_snapshot, analyzed_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, datetime('now')
        )
        """,
        (
            case.get("forensic_id") or str(uuid.uuid4()),
            case["signal_id"],
            case["tracking_id"],
            case["setup_id"],
            case["symbol"],
            case["direction"],
            case["timeframe"],
            case.get("signal_created_at"),
            case.get("entry_activated_at"),
            case.get("sl_hit_at"),
            case.get("time_to_entry_min"),
            case.get("time_to_sl_min"),
            case.get("entry_price"),
            case.get("sl_price"),
            case.get("tp1_price"),
            case.get("tp2_price"),
            case.get("sl_distance_pct"),
            case.get("rr_ratio"),
            case.get("post_sl_candles_analyzed"),
            case.get("post_sl_max_adverse_pct"),
            case.get("post_sl_max_recovery_pct"),
            1 if case.get("post_sl_tp1_reached") else 0,
            case.get("post_sl_tp1_candles"),
            case.get("post_sl_price_at_close"),
            case.get("btc_move_in_sl_candle_pct"),
            case.get("btc_direction_match"),
            1 if case.get("btc_caused_sl") else 0,
            case.get("score"),
            case.get("atr_pct"),
            case.get("spread_bps"),
            case.get("funding_rate"),
            case.get("entry_deviation_atr_mult"),
            case.get("entry_candle_was_confirmed"),
            case.get("market_regime"),
            case.get("btc_bias"),
            case.get("direction_vs_bias"),
            case.get("sl_type"),
            case.get("sl_subtype"),
            case.get("sl_verdict"),
            case.get("candles_signal_tf"),
            case.get("candles_1h"),
            case.get("candles_4h"),
            case.get("candles_btc_signal"),
            case.get("strategy_recheck_valid"),
            json.dumps(case.get("indicator_snapshot") or {}),
        ),
    )
    await conn.commit()


def _append_monitoring_hook(report_summary: str) -> None:
    path = ROOT / "REPORT_SL_ANALYSIS.md"
    if not path.exists():
        return
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    block = f"\n\n---\n\n## Forensic snapshot — {stamp}\n\n{report_summary}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(block)


def _print_summary(cases: list[dict[str, Any]]) -> None:
    total = len(cases)
    if total == 0:
        print("No SL cases analyzed.")
        return

    type_counts: dict[str, int] = {}
    subtype_counts: dict[str, int] = {}
    for case in cases:
        sl_type = str(case.get("sl_type") or "UNKNOWN")
        subtype = str(case.get("sl_subtype") or "")
        type_counts[sl_type] = type_counts.get(sl_type, 0) + 1
        key = f"{sl_type}/{subtype}" if subtype else sl_type
        subtype_counts[key] = subtype_counts.get(key, 0) + 1

    print(f"\nTotal analyzed: {total}")
    for sl_type in ("STOP_HUNT", "IMMEDIATE_ADVERSE", "THESIS_FAILED", "TIMING_OFF"):
        count = type_counts.get(sl_type, 0)
        pct = count / total * 100.0
        print(f"  {sl_type}: {count} ({pct:.1f}%)")
        if sl_type == "IMMEDIATE_ADVERSE":
            for sub in (
                "BTC_DRAG",
                "ENTRY_CHASE",
                "FALSE_SIGNAL",
                "REGIME_FADE",
                "IMMEDIATE_ADVERSE",
            ):
                sub_key = f"IMMEDIATE_ADVERSE/{sub}"
                if sub_key in subtype_counts:
                    print(f"    - {sub}: {subtype_counts[sub_key]}")

    fixable = [
        (c.get("sl_type"), c.get("sl_subtype"))
        for c in cases
        if c.get("sl_type") in {"STOP_HUNT", "IMMEDIATE_ADVERSE"}
    ]
    if fixable:
        common_type, count = Counter(fixable).most_common(1)[0]
        print(f"\nMost common fixable type: {common_type[0]}/{common_type[1]} ({count} cases)")


async def main() -> int:
    _setup_logging()
    args = _parse_args()
    settings = load_settings()
    db_path = Path(getattr(settings, "db_path", ROOT / "data/bot/bot.db"))

    async with aiosqlite.connect(db_path) as conn:
        applied = await migrate_db(conn)
        if applied:
            LOG.info("Applied %d migration(s)", applied)

        if args.report_only:
            cases = await _load_cached_cases(conn, args.days)
            print(f"{len(cases)} SL cases found in sl_forensics (report-only)")
        else:
            rows = await _fetch_sl_rows(conn, days=args.days, signal_id=args.signal_id)
            if not rows:
                print(f"No SL cases in last {args.days} day(s)")
                return 0
            print(f"{len(rows)} SL cases found")

            cases = []
            fetcher = CandleFetcher()
            try:
                for idx, row in enumerate(rows, start=1):
                    tracking_id = str(row["tracking_id"])
                    symbol = str(row["symbol"])
                    setup_id = str(row["setup_id"])
                    print(f"Analyzing [{idx}/{len(rows)}] {symbol} {setup_id}...")

                    existing = await _existing_forensic(conn, tracking_id)
                    if existing and args.report_only:
                        cases.append(existing)
                        continue

                    case = _row_to_base_case(row)
                    case["forensic_id"] = str(uuid.uuid4())

                    signal_ts = ts_ms_from_iso(case["signal_created_at"])
                    sl_ts = ts_ms_from_iso(case["sl_hit_at"])
                    anchor_ts = signal_ts or sl_ts
                    if anchor_ts is None:
                        LOG.warning(
                            "skip candle fetch | tracking_id=%s symbol=%s "
                            "reason=missing_timestamps",
                            tracking_id,
                            symbol,
                        )
                        continue

                    try:
                        tf = case["timeframe"].split("+")[0].strip() or "15m"
                        windows = await fetcher.fetch_window(
                            symbol,
                            anchor_ts_ms=anchor_ts,
                            sl_hit_ts_ms=sl_ts or anchor_ts,
                            signal_tf=tf,
                        )
                        signal_candles = windows.get(tf, [])
                        btc_candles = windows.get("BTC_signal_tf", [])

                        post = compute_post_sl_action(
                            signal_candles,
                            sl_ts or anchor_ts,
                            float(case["entry_price"] or 0),
                            float(case["sl_price"] or 0),
                            float(case["tp1_price"] or 0),
                            str(case["direction"]),
                        )
                        case.update(post)

                        btc = compute_btc_correlation(
                            signal_candles,
                            btc_candles,
                            sl_ts or anchor_ts,
                        )
                        case.update(btc)

                        case["entry_deviation_atr_mult"] = compute_entry_deviation(
                            signal_candles,
                            float(case["entry_price"] or 0),
                            signal_ts or anchor_ts,
                            float(case["atr_pct"] or 0),
                            activation_ts_ms=ts_ms_from_iso(case.get("entry_activated_at")),
                        )

                        act_ts = ts_ms_from_iso(case.get("entry_activated_at")) or signal_ts
                        if act_ts is not None:
                            closed_valid = assess_closed_candle_validity(
                                signal_candles,
                                event_ts_ms=act_ts,
                                direction=str(case["direction"]),
                            )
                            case["closed_candle_valid"] = (
                                1 if closed_valid else 0 if closed_valid is False else None
                            )
                            confirmed = infer_confirmed_candle(
                                setup_id=str(case["setup_id"]),
                                signal_created_at=case.get("signal_created_at"),
                                features_snapshot=case.get("indicator_snapshot"),
                                assess_closed_valid=closed_valid,
                            )
                            case["confirmed_candle"] = confirmed
                            case["entry_candle_was_confirmed"] = (
                                confirmed if confirmed is not None else 0
                            )
                            snap = case.get("indicator_snapshot") or {}
                            if isinstance(snap, dict):
                                snap["closed_candle_valid"] = case["closed_candle_valid"]
                                snap["confirmed_candle"] = confirmed
                                snap["mfe"] = case.get("mfe")
                                snap["mae"] = case.get("mae")
                                case["indicator_snapshot"] = snap

                        case["candles_signal_tf"] = _candles_json(signal_candles)
                        case["candles_1h"] = _candles_json(windows.get("1h"))
                        case["candles_4h"] = _candles_json(windows.get("4h"))
                        case["candles_btc_signal"] = _candles_json(btc_candles)

                        if args.recheck or not existing:
                            recheck = await recheck_strategy(
                                setup_id,
                                symbol,
                                case["timeframe"],
                                signal_candles,
                                signal_ts or anchor_ts,
                                settings,
                                context=case,
                            )
                            valid = recheck.get("valid")
                            if valid is True:
                                case["strategy_recheck_valid"] = 1
                            elif valid is False:
                                case["strategy_recheck_valid"] = 0
                            else:
                                case["strategy_recheck_valid"] = None
                            case["strategy_recheck_reason"] = recheck.get("reason")
                        elif existing:
                            case["strategy_recheck_valid"] = existing.get("strategy_recheck_valid")
                        else:
                            case["strategy_recheck_valid"] = None

                        if case.get("confirmed_candle") is None:
                            confirmed = infer_confirmed_candle(
                                setup_id=str(case["setup_id"]),
                                signal_created_at=case.get("signal_created_at"),
                                features_snapshot=case.get("indicator_snapshot"),
                                assess_closed_valid=(
                                    None
                                    if case.get("closed_candle_valid") is None
                                    else case.get("closed_candle_valid") == 1
                                ),
                            )
                            case["confirmed_candle"] = confirmed
                            case["entry_candle_was_confirmed"] = (
                                confirmed if confirmed is not None else 0
                            )

                        sl_type, sl_subtype, sl_verdict = classify_sl(case)
                        case["sl_type"] = sl_type
                        case["sl_subtype"] = sl_subtype
                        case["sl_verdict"] = sl_verdict

                        await _insert_forensic(conn, case)
                        cases.append(case)
                        await asyncio.sleep(0.2)
                    except Exception:
                        LOG.exception(
                            "case failed | tracking_id=%s symbol=%s",
                            tracking_id,
                            symbol,
                        )
                        continue
            finally:
                await fetcher.close()

    report_path = ROOT / "REPORT_SL_FORENSIC.md"
    # Deduplicate by tracking_id (latest analysis wins).
    deduped: dict[str, dict[str, Any]] = {}
    for case in cases:
        tid = str(case.get("tracking_id") or "")
        if tid:
            deduped[tid] = case
    cases = list(deduped.values())
    cases.sort(key=lambda c: str(c.get("sl_hit_at") or ""), reverse=True)
    report_md = generate_aggregate_report(cases)
    report_path.write_text(report_md, encoding="utf-8")
    print(f"\nReport written: {report_path}")

    summary_lines = []
    type_counts = Counter(str(c.get("sl_type") or "UNKNOWN") for c in cases)
    for sl_type, count in type_counts.most_common():
        summary_lines.append(f"- {sl_type}: {count}")
    _append_monitoring_hook("\n".join(summary_lines))

    _print_summary(cases)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None

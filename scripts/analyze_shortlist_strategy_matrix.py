"""Shortlist ↔ strategy fit matrix (static + live telemetry).

Usage:
  python scripts/analyze_shortlist_strategy_matrix.py --live-static
  python scripts/analyze_shortlist_strategy_matrix.py --run-id 20260602T190450Z
  python scripts/analyze_shortlist_strategy_matrix.py --telemetry-run 20260602T190450Z --live-static
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from scripts.common import bootstrap_repo_path, configure_script_logging
except ModuleNotFoundError:
    from common import bootstrap_repo_path, configure_script_logging

bootstrap_repo_path()

from bot.domain.config import _ALL_SETUP_IDS, load_settings
from bot.market.fit import ASSET_FIT_PROFILES, asset_fit_for_strategy
from bot.market.rest_impl import BinanceClientImpl
from bot.market.universe import build_shortlist

LOG = configure_script_logging("scripts.analyze_shortlist_strategy_matrix")

# From STRATEGY_CATALOG.md summary matrix (Binance public data column).
STRATEGY_DATA_NEEDS: dict[str, str] = {
    "structure_pullback": "klines",
    "structure_break_retest": "klines",
    "wick_trap_reversal": "klines",
    "squeeze_setup": "klines",
    "ema_bounce": "klines",
    "fvg_setup": "klines",
    "order_block": "klines",
    "liquidity_sweep": "klines",
    "bos_choch": "klines",
    "hidden_divergence": "klines",
    "indicator_divergence": "klines+aggTrade",
    "funding_reversal": "funding+OI",
    "cvd_divergence": "aggTrade",
    "session_killzone": "klines+time",
    "breaker_block": "klines",
    "turtle_soup": "klines",
    "vwap_trend": "klines",
    "supertrend_follow": "klines",
    "multi_tf_trend": "klines",
    "price_velocity": "klines",
    "volume_anomaly": "klines",
    "volume_climax_reversal": "klines",
    "keltner_breakout": "klines",
    "bb_squeeze": "klines",
    "atr_expansion": "klines",
    "whale_walls": "depth",
    "spread_strategy": "bookTicker",
    "depth_imbalance": "depth",
    "absorption": "aggTrade",
    "aggression_shift": "aggTrade+taker",
    "liquidation_heatmap": "forceOrder+OI",
    "stop_hunt_detection": "klines",
    "oi_divergence": "OI+hist",
    "ls_ratio_extreme": "global L/S",
    "rsi_divergence_bottom": "klines",
    "wyckoff_spring": "klines",
    "btc_correlation": "multi klines",
    "altcoin_season_index": "ticker24h",
}

DATA_PLANE_NOTES = {
    "klines": "all shortlist @15m + REST context",
    "bookTicker": "global !bookTicker",
    "depth": "top-20 shortlist only (depth_symbol_limit)",
    "aggTrade": "tracked/active symbols only",
    "funding+OI": "REST batch 5-15m on shortlist",
    "OI+hist": "REST batch on shortlist",
    "forceOrder+OI": "global forceOrder + OI batch",
    "aggTrade+taker": "aggTrade tracked + taker REST",
    "global L/S": "REST /futures/data on shortlist",
    "multi klines": "BTC + alt klines on shortlist",
    "ticker24h": "global ticker / light pool",
    "klines+time": "klines + session clock",
    "klines+aggTrade": "klines all; aggTrade tracked subset",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _find_telemetry_run(telemetry_root: Path, run_id: str | None) -> Path | None:
    runs_dir = telemetry_root / "runs"
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


def _static_fit_score(
    setup_id: str,
    *,
    symbols_with_heuristic_fit: int,
    shortlist_size: int,
    depth_slots: int,
    agg_trade_scope: str,
) -> tuple[int, str]:
    """1=poor .. 5=excellent shortlist alignment for this strategy."""
    need = STRATEGY_DATA_NEEDS.get(setup_id, "klines")
    coverage = symbols_with_heuristic_fit / max(shortlist_size, 1)
    profile = asset_fit_for_strategy(setup_id)

    penalty = 0
    notes: list[str] = []
    if "depth" in need and depth_slots < shortlist_size:
        penalty += 1
        notes.append(f"depth top-{depth_slots}")
    if "aggTrade" in need and agg_trade_scope != "full_shortlist":
        penalty += 1
        notes.append("aggTrade tracked-only")
    if profile.requires_oi or profile.requires_funding:
        notes.append("needs REST OI/funding batch")
    if setup_id == "btc_correlation":
        penalty += 1
        notes.append("needs BTC+all pairs context")
    if setup_id == "altcoin_season_index":
        notes.append("macro ticker scan")

    if coverage >= 0.8 and penalty == 0:
        score = 5
    elif coverage >= 0.5 and penalty <= 1:
        score = 4
    elif coverage >= 0.25 or symbols_with_heuristic_fit >= 3:
        score = 3
    elif symbols_with_heuristic_fit > 0:
        score = 2
    else:
        score = 1
    return score, "; ".join(notes) if notes else "ok"


async def _build_static_matrix(config_path: Path) -> dict[str, Any]:
    settings = load_settings(config_path)
    client = BinanceClientImpl(
        network=settings.network,
        rest_timeout_seconds=float(settings.ws.rest_timeout_seconds),
    )
    try:
        symbol_meta = await client.fetch_exchange_symbols()
        tickers = await client.fetch_ticker_24h()
    finally:
        await client.close()

    shortlist, summary = build_shortlist(
        list(symbol_meta),
        list(tickers),
        settings,
        seed_source="matrix_static",
    )
    depth_limit = int(getattr(settings.ws, "depth_symbol_limit", 20))
    by_strategy: dict[str, list[str]] = defaultdict(list)
    for item in shortlist:
        for setup_id in item.strategy_fits:
            by_strategy[setup_id].append(item.symbol)

    rows: list[dict[str, Any]] = []
    for setup_id in _ALL_SETUP_IDS:
        syms = by_strategy.get(setup_id, [])
        score, note = _static_fit_score(
            setup_id,
            symbols_with_heuristic_fit=len(syms),
            shortlist_size=len(shortlist),
            depth_slots=depth_limit,
            agg_trade_scope="tracked_only",
        )
        profile = ASSET_FIT_PROFILES.get(setup_id)
        rows.append(
            {
                "setup_id": setup_id,
                "data_need": STRATEGY_DATA_NEEDS.get(setup_id, "?"),
                "data_plane": DATA_PLANE_NOTES.get(
                    STRATEGY_DATA_NEEDS.get(setup_id, "klines"),
                    "see catalog",
                ),
                "heuristic_symbols": len(syms),
                "heuristic_pct": round(100.0 * len(syms) / max(len(shortlist), 1), 1),
                "min_liq_rank": profile.min_liquidity_rank if profile else 100,
                "requires_oi": bool(profile and profile.requires_oi),
                "requires_funding": bool(profile and profile.requires_funding),
                "static_fit_score": score,
                "static_notes": note,
                "sample_symbols": syms[:5],
            }
        )

    return {
        "shortlist_size": len(shortlist),
        "summary": summary,
        "depth_symbol_limit": depth_limit,
        "static_rows": rows,
    }


def _analyze_live_telemetry(analysis_dir: Path) -> dict[str, Any]:
    decisions = _read_jsonl(analysis_dir / "strategy_decisions.jsonl")
    shortlist_rows = _read_jsonl(analysis_dir / "shortlist.jsonl")
    latest_shortlist = shortlist_rows[-1] if shortlist_rows else {}
    fit_counts = latest_shortlist.get("strategy_fit_counts") or {}

    by_strategy: dict[str, Counter[str]] = defaultdict(Counter)
    runs_by_strategy: Counter[str] = Counter()
    symbols_by_strategy: dict[str, set[str]] = defaultdict(set)

    for row in decisions:
        setup_id = str(row.get("setup_id") or row.get("strategy") or "").strip()
        if not setup_id:
            continue
        decision = str(row.get("decision") or "").lower()
        reason = str(row.get("reason_code") or row.get("skip_reason") or "unknown")
        symbol = str(row.get("symbol") or "").upper()
        runs_by_strategy[setup_id] += 1
        if symbol:
            symbols_by_strategy[setup_id].add(symbol)
        by_strategy[setup_id][decision] += 1
        if decision in {"skip", "skipped"}:
            by_strategy[setup_id][f"skip:{reason}"] += 1

    live_rows: list[dict[str, Any]] = []
    for setup_id in _ALL_SETUP_IDS:
        counts = by_strategy.get(setup_id, Counter())
        skips = sum(v for k, v in counts.items() if k.startswith("skip:"))
        top_skip = ""
        skip_items = [
            (k.removeprefix("skip:"), v) for k, v in counts.items() if k.startswith("skip:")
        ]
        if skip_items:
            top_skip = max(skip_items, key=lambda x: x[1])[0]
        live_rows.append(
            {
                "setup_id": setup_id,
                "live_runs": runs_by_strategy.get(setup_id, 0),
                "live_symbols": len(symbols_by_strategy.get(setup_id, set())),
                "signals": counts.get("signal", 0),
                "rejects": counts.get("reject", 0) + counts.get("rejected", 0),
                "skips": skips,
                "top_skip_reason": top_skip,
                "shortlist_fit_count": int(fit_counts.get(setup_id, 0) or 0),
            }
        )

    return {
        "decision_rows": len(decisions),
        "shortlist_telemetry_rows": len(shortlist_rows),
        "latest_shortlist": latest_shortlist,
        "live_rows": live_rows,
    }


def _merge_rows(
    static_rows: list[dict[str, Any]], live_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    live_map = {row["setup_id"]: row for row in live_rows}
    merged: list[dict[str, Any]] = []
    for srow in static_rows:
        setup_id = srow["setup_id"]
        lrow = live_map.get(setup_id, {})
        merged.append({**srow, **lrow})
    return merged


def _verdict(row: dict[str, Any]) -> str:
    static = int(row.get("static_fit_score") or 0)
    runs = int(row.get("live_runs") or 0)
    heuristic = int(row.get("heuristic_symbols") or 0)
    if runs == 0 and heuristic == 0:
        return "gap"
    if static >= 4 and runs >= 5:
        return "good"
    if static >= 3 and runs >= 1:
        return "ok"
    if runs >= 1:
        return "partial"
    return "weak"


def _print_markdown_table(merged: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    print("# Shortlist ↔ Strategy Fit Matrix\n")
    print(f"- Shortlist size: **{meta.get('shortlist_size', '?')}**")
    print(
        f"- Gate passed / light pool: **{meta.get('gate_passed', '?')}** / **{meta.get('light_pool', '?')}**"
    )
    print(f"- Live decision rows: **{meta.get('decision_rows', 0)}**")
    print(f"- Telemetry run: `{meta.get('run_id', 'static-only')}`")
    print()
    print(
        "| # | setup_id | data | static | heur.syms | live runs | syms | signal | skip | top skip | verdict |"
    )
    print("|---:|---|---|---:|---:|---:|---:|---:|---:|---|---|")
    for idx, row in enumerate(merged, start=1):
        print(
            f"| {idx} | `{row['setup_id']}` | {row.get('data_need', '?')} "
            f"| {row.get('static_fit_score', '-')} "
            f"| {row.get('heuristic_symbols', 0)} "
            f"| {row.get('live_runs', 0)} "
            f"| {row.get('live_symbols', 0)} "
            f"| {row.get('signals', 0)} "
            f"| {row.get('skips', 0)} "
            f"| {row.get('top_skip_reason', '') or '-'} "
            f"| **{_verdict(row)}** |"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shortlist strategy fit matrix")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--live-static", action="store_true", help="Build shortlist from live REST")
    parser.add_argument("--run-id", type=str, default="", help="Bot telemetry run id")
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--markdown", action="store_true", default=True)
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    static: dict[str, Any] = {}
    live: dict[str, Any] = {}

    if args.live_static:
        static = asyncio.run(_build_static_matrix(args.config))
    else:
        static = {"static_rows": [], "shortlist_size": 0, "summary": {}}

    run_dir = _find_telemetry_run(settings.telemetry_dir, args.run_id or None)
    if run_dir is not None:
        live = _analyze_live_telemetry(run_dir / "analysis")
    elif not args.live_static:
        print("No telemetry run found", file=sys.stderr)
        return 1

    merged = _merge_rows(static.get("static_rows", []), live.get("live_rows", []))
    meta = {
        "shortlist_size": static.get("shortlist_size")
        or live.get("latest_shortlist", {}).get("size"),
        "gate_passed": static.get("summary", {}).get("gate_passed")
        or live.get("latest_shortlist", {}).get("gate_passed"),
        "light_pool": static.get("summary", {}).get("light_pool")
        or live.get("latest_shortlist", {}).get("light_pool"),
        "decision_rows": live.get("decision_rows", 0),
        "run_id": run_dir.name if run_dir else None,
    }

    report = {"meta": meta, "static": static, "live": live, "merged": merged}
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    if args.markdown:
        _print_markdown_table(merged, meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

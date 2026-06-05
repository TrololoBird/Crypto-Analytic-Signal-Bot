"""Strategy vs shortlist fit matrix — static theory + live telemetry.

Usage:
  python scripts/strategy_shortlist_matrix.py --static
  python scripts/strategy_shortlist_matrix.py --run-id 20260602T190450Z
  python scripts/strategy_shortlist_matrix.py --live-shortlist
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

try:
    from scripts.common import configure_script_logging
except ModuleNotFoundError:  # pragma: no cover
    from common import configure_script_logging

from bot.diagnostics.facade import (
    analyze_telemetry,
    find_live_watch_session,
    resolve_telemetry_analysis_dir,
    summarize_live_watch_session,
)
from bot.domain.config import _ALL_SETUP_IDS, load_settings
from bot.domain.strategy_catalog import CATALOG_BY_ID
from bot.market.fit import ASSET_FIT_PROFILES
from bot.market.rest_impl import BinanceClientImpl
from bot.market.universe import build_shortlist
from bot.runtime.errors import DEFENSIVE_EXC

LOG = configure_script_logging("scripts.strategy_shortlist_matrix")

# Catalog → primary public data plane (signal-only, no private API).
DATA_NEED: dict[str, str] = {
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
    "funding_reversal": "funding+OI REST",
    "cvd_divergence": "aggTrade",
    "session_killzone": "klines+clock",
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
    "whale_walls": "depth top-20",
    "spread_strategy": "bookTicker",
    "depth_imbalance": "depth top-20",
    "absorption": "aggTrade tracked",
    "aggression_shift": "aggTrade+taker REST",
    "liquidation_heatmap": "forceOrder+OI",
    "stop_hunt_detection": "klines",
    "oi_divergence": "OI REST",
    "ls_ratio_extreme": "L/S REST",
    "rsi_divergence_bottom": "klines",
    "wyckoff_spring": "klines",
    "btc_correlation": "BTC+alt klines",
    "altcoin_season_index": "ticker24h global",
}

WS_TIER: dict[str, str] = {
    "klines": "shortlist @kline_15m",
    "klines+aggTrade": "shortlist klines; aggTrade tracked",
    "aggTrade": "tracked symbols only",
    "aggTrade tracked": "tracked symbols only",
    "aggTrade+taker REST": "aggTrade tracked + REST batch",
    "depth top-20": "depth top-20 by score",
    "bookTicker": "global !bookTicker",
    "funding+OI REST": "REST batch 5-15m shortlist",
    "OI REST": "REST batch shortlist",
    "L/S REST": "REST batch shortlist",
    "forceOrder+OI": "global forceOrder + OI REST",
    "klines+clock": "shortlist klines",
    "BTC+alt klines": "shortlist + BTC anchor",
    "ticker24h global": "global !ticker@arr",
}


def _heuristic_fit_band(setup_id: str) -> str:
    """How often dynamic (non-pinned) symbols get this fit from universe heuristics."""
    trend = {
        "ema_bounce",
        "structure_pullback",
        "vwap_trend",
        "supertrend_follow",
        "multi_tf_trend",
        "fvg_setup",
        "cvd_divergence",
        "btc_correlation",
        "altcoin_season_index",
    }
    breakout = {
        "structure_break_retest",
        "squeeze_setup",
        "bb_squeeze",
        "atr_expansion",
        "bos_choch",
        "order_block",
        "breaker_block",
        "session_killzone",
        "price_velocity",
        "volume_anomaly",
        "keltner_breakout",
        "spread_strategy",
        "depth_imbalance",
        "whale_walls",
        "aggression_shift",
    }
    reversal = {
        "wick_trap_reversal",
        "hidden_divergence",
        "rsi_divergence_bottom",
        "turtle_soup",
        "liquidity_sweep",
        "stop_hunt_detection",
        "wyckoff_spring",
        "liquidation_heatmap",
        "absorption",
        "volume_climax_reversal",
    }
    positioning = {"funding_reversal", "ls_ratio_extreme", "oi_divergence"}
    if setup_id in trend:
        return "trend bucket"
    if setup_id in breakout:
        return "breakout bucket"
    if setup_id in reversal:
        return "reversal bucket"
    if setup_id in positioning:
        return "positioning gate"
    if setup_id == "indicator_divergence":
        return "top-liq only"
    return "fallback/pinned"


def _theoretical_score(_setup_id: str, data_need: str) -> tuple[int, str]:
    """1=poor .. 5=excellent shortlist alignment for signal-only bot."""
    if data_need == "klines":
        return 5, "full shortlist klines"
    if data_need in {"bookTicker", "ticker24h global"}:
        return 5, "global WS, all symbols"
    if data_need in {"funding+OI REST", "OI REST", "L/S REST"}:
        return 4, "REST batch on shortlist OK"
    if data_need == "klines+clock":
        return 5, "klines + session clock"
    if data_need == "BTC+alt klines":
        return 4, "needs BTC anchor always pinned"
    if "depth top-20" in data_need:
        return 3, "only top-20 depth slots"
    if "aggTrade" in data_need:
        return 2, "aggTrade not on full shortlist"
    if data_need == "forceOrder+OI":
        return 4, "forceOrder global; OI REST"
    return 3, "mixed"


def build_static_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for setup_id in _ALL_SETUP_IDS:
        entry = CATALOG_BY_ID.get(setup_id)
        data_need = DATA_NEED.get(setup_id, "klines")
        score, score_note = _theoretical_score(setup_id, data_need)
        profile = ASSET_FIT_PROFILES.get(setup_id)
        rows.append(
            {
                "setup_id": setup_id,
                "evidence": entry.evidence_level if entry else "?",
                "family": entry.family if entry else "?",
                "data_need": data_need,
                "ws_tier": WS_TIER.get(data_need, "shortlist"),
                "fit_heuristic": _heuristic_fit_band(setup_id),
                "requires_oi": bool(profile and profile.requires_oi),
                "requires_funding": bool(profile and profile.requires_funding),
                "min_liq_rank": profile.min_liquidity_rank if profile else 100,
                "fit_score": score,
                "fit_note": score_note,
            }
        )
    return rows


async def _warm_basis_cache(
    client: BinanceClientImpl,
    symbols: list[str],
    *,
    limit: int,
) -> dict[str, int]:
    warmed = 0
    failed = 0
    for symbol in symbols[:limit]:
        try:
            await client.fetch_basis(symbol, period="1h", limit=5)
            await client.fetch_basis(symbol, period="5m", limit=5)
            warmed += 1
        except DEFENSIVE_EXC:
            failed += 1
    return {
        "basis_warm_attempted": min(len(symbols), limit),
        "basis_warm_ok": warmed,
        "basis_warm_failed": failed,
    }


async def live_shortlist_fit_counts(
    config_path: Path,
    *,
    include_basis: bool = False,
    basis_warm_limit: int = 25,
) -> dict[str, Any]:
    settings = load_settings(config_path)
    client = BinanceClientImpl(
        network=settings.network,
        rest_timeout_seconds=float(settings.ws.rest_timeout_seconds),
    )
    try:
        meta = await client.fetch_exchange_symbols()
        tickers = await client.fetch_ticker_24h()
        shortlist, summary = build_shortlist(
            list(meta), list(tickers), settings, seed_source="matrix_live"
        )
        basis_warm: dict[str, int] = {}
        if include_basis and shortlist:
            basis_warm = await _warm_basis_cache(
                client,
                [item.symbol for item in shortlist],
                limit=basis_warm_limit,
            )
    finally:
        await client.close()
    fit_counts = summary.get("strategy_fit_counts") or {}
    symbol_fits = {item.symbol: list(item.strategy_fits) for item in shortlist}
    return {
        "shortlist_size": len(shortlist),
        "gate_passed": int(summary.get("gate_passed") or 0),
        "light_pool": int(summary.get("light_pool") or 0),
        "fit_density": summary.get("strategy_fit_density"),
        "fit_counts": {k: int(v) for k, v in fit_counts.items()},
        "symbol_fits": symbol_fits,
        "summary": summary,
        "basis_warm": basis_warm,
    }


def _print_static_table(rows: list[dict[str, Any]]) -> None:
    print("\n## Static: strategy × shortlist fit (theory)\n")
    print("| # | setup_id | data | WS tier | heuristic | OI | fund | fit | note |")
    print("|---|----------|------|---------|-----------|----|------|-----|------|")
    for idx, row in enumerate(rows, start=1):
        oi = "Y" if row["requires_oi"] else "-"
        fr = "Y" if row["requires_funding"] else "-"
        print(
            f"| {idx} | {row['setup_id']} | {row['data_need']} | {row['ws_tier']} | "
            f"{row['fit_heuristic']} | {oi} | {fr} | {row['fit_score']}/5 | {row['fit_note']} |"
        )
    avg = sum(r["fit_score"] for r in rows) / max(len(rows), 1)
    print(f"\nAverage theoretical fit score: **{avg:.2f}/5**")


def _print_live_table(live: dict[str, Any], static: list[dict[str, Any]]) -> None:
    static_map = {r["setup_id"]: r for r in static}
    print("\n## Live: strategy runs (15m telemetry)\n")
    print(
        "| setup_id | theory | shortlist symbols w/ fit | symbols ran | "
        "signal | reject | skip | not_routed |"
    )
    print(
        "|----------|--------|--------------------------|-------------|--------|--------|------|------------|"
    )
    for row in live["strategies"]:
        sid = row["setup_id"]
        theory = static_map.get(sid, {}).get("fit_score", "?")
        print(
            f"| {sid} | {theory}/5 | {row['shortlist_fit_symbols']} | "
            f"{row['symbols_touched']} | {row['signal']} | {row['reject']} | "
            f"{row['skip']} | {row['not_routed']} |"
        )
    ls = live["latest_shortlist"]
    print(
        f"\nSession: decisions={live['decision_rows']} "
        f"strategies_ran={live['strategies_ran']}/38 "
        f"not_routed_skips={live['skip_not_routed_total']}"
    )
    if ls:
        print(
            f"Shortlist funnel: gate_passed={ls.get('gate_passed')} "
            f"light_pool={ls.get('light_pool')}/{ls.get('light_pool_limit')} "
            f"size={ls.get('size')} fit_density={ls.get('strategy_fit_density')}"
        )
    if live["strategies_zero_runs"]:
        print(f"Zero runs: {', '.join(live['strategies_zero_runs'])}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--static", action="store_true")
    parser.add_argument("--live-shortlist", action="store_true")
    parser.add_argument("--run-id", type=str, default="")
    parser.add_argument(
        "--live-watch-dir",
        type=Path,
        default=Path("data/live_watch"),
        help="Supervised session root when telemetry/runs has no analysis JSONL",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--include-basis",
        action="store_true",
        help="Warm /futures/data/basis cache for shortlist symbols (calibration runs)",
    )
    parser.add_argument(
        "--basis-warm-limit",
        type=int,
        default=25,
        help="Max shortlist symbols to warm basis for when --include-basis is set",
    )
    args = parser.parse_args()

    static_rows = build_static_rows()
    report: dict[str, Any] = {"static": static_rows}

    if (args.static or (not args.run_id and not args.live_shortlist)) and not args.json:
        _print_static_table(static_rows)

    if args.live_shortlist:
        live_sl = asyncio.run(
            live_shortlist_fit_counts(
                args.config,
                include_basis=args.include_basis,
                basis_warm_limit=args.basis_warm_limit,
            )
        )
        report["live_shortlist"] = live_sl
        if not args.json:
            print("\n## Live shortlist fit counts\n")
            for sid in _ALL_SETUP_IDS:
                print(f"  {sid}: {live_sl['fit_counts'].get(sid, 0)} symbols")

    if args.run_id or (not args.static and not args.live_shortlist):
        settings = load_settings(args.config)
        runs_dir = settings.telemetry_dir / "runs"
        run_id = args.run_id
        if not run_id:
            lw_candidates = sorted(
                (
                    p
                    for p in args.live_watch_dir.iterdir()
                    if p.is_dir() and not p.name.startswith("rollup_")
                ),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            candidates = (
                sorted(
                    (p for p in runs_dir.iterdir() if p.is_dir()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if runs_dir.exists()
                else []
            )
            if candidates:
                run_id = candidates[0].name
            elif lw_candidates:
                run_id = lw_candidates[0].name
            else:
                if args.json:
                    print(json.dumps({"error": "no_telemetry_or_live_watch_runs"}, indent=2))
                else:
                    print("No telemetry or live_watch runs found")
                return 1
        analysis_dir, source = resolve_telemetry_analysis_dir(
            run_id=run_id,
            telemetry_dir=settings.telemetry_dir,
            live_watch_dir=args.live_watch_dir,
        )
        if analysis_dir is not None:
            live = analyze_telemetry(analysis_dir)
            report["telemetry_source"] = source
        else:
            session = find_live_watch_session(args.live_watch_dir, run_id)
            if session is None:
                if args.json:
                    print(json.dumps({"error": "run_not_found", "run_id": run_id}, indent=2))
                else:
                    print(f"Run not found: {run_id}")
                return 1
            live = summarize_live_watch_session(session, telemetry_dir=settings.telemetry_dir)
            report["telemetry_source"] = "live_watch"
        report["live_telemetry"] = live
        report["run_id"] = run_id
        if not args.json and analysis_dir is not None:
            _print_live_table(live, static_rows)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

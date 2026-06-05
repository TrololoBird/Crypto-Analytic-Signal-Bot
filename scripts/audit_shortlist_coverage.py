"""Audit shortlist funnel coverage and strategy fit density.

Usage:
  python scripts/audit_shortlist_coverage.py --live
  python scripts/audit_shortlist_coverage.py --run-id 20260602T190450Z
  python scripts/audit_shortlist_coverage.py --config config.toml --live --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from scripts.common import bootstrap_repo_path, configure_script_logging
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from common import bootstrap_repo_path, configure_script_logging

bootstrap_repo_path()

from bot.domain.config import _ALL_SETUP_IDS, load_settings
from bot.market.rest_impl import BinanceClientImpl
from bot.market.universe import build_shortlist

LOG = configure_script_logging("scripts.audit_shortlist_coverage")


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


def _latest_run_dir(telemetry_root: Path, run_id: str | None) -> Path | None:
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


def _analyze_telemetry(analysis_dir: Path) -> dict[str, Any]:
    shortlist_rows = _read_jsonl(analysis_dir / "shortlist.jsonl")
    build_rows = _read_jsonl(analysis_dir / "shortlist_build.jsonl")
    decision_rows = _read_jsonl(analysis_dir / "strategy_decisions.jsonl")

    latest_shortlist = shortlist_rows[-1] if shortlist_rows else {}
    latest_build = build_rows[-1] if build_rows else {}

    skip_reasons = Counter(
        str(row.get("reason_code") or row.get("skip_reason") or "unknown")
        for row in decision_rows
        if str(row.get("decision") or "").lower() in {"skip", "skipped"}
    )
    routed = Counter(
        str(row.get("setup_id") or row.get("strategy") or "unknown")
        for row in decision_rows
        if str(row.get("decision") or "").lower() in {"signal", "reject", "candidate"}
    )

    fit_counts = latest_shortlist.get("strategy_fit_counts") or {}
    missing_strategies = [
        setup_id for setup_id in _ALL_SETUP_IDS if int(fit_counts.get(setup_id, 0) or 0) == 0
    ]

    return {
        "telemetry_shortlist_rows": len(shortlist_rows),
        "telemetry_build_rows": len(build_rows),
        "latest_shortlist": latest_shortlist,
        "latest_build": latest_build,
        "decision_rows": len(decision_rows),
        "top_skip_reasons": skip_reasons.most_common(10),
        "strategy_runs": len(routed),
        "strategies_with_runs": len(routed),
        "strategies_without_shortlist_fit": missing_strategies[:20],
        "strategies_without_shortlist_fit_count": len(missing_strategies),
    }


async def _live_shortlist_audit(config_path: Path) -> dict[str, Any]:
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
        seed_source="audit_live",
    )
    fit_counts = summary.get("strategy_fit_counts") or {}
    missing_strategies = [
        setup_id for setup_id in _ALL_SETUP_IDS if int(fit_counts.get(setup_id, 0) or 0) == 0
    ]
    return {
        "mode": "live",
        "config": str(config_path),
        "raw_tickers": len(tickers),
        "gate_passed": summary.get("gate_passed"),
        "light_pool": summary.get("light_pool"),
        "light_pool_limit": summary.get("light_pool_limit"),
        "eligible": summary.get("eligible"),
        "dynamic_pool": summary.get("dynamic_pool"),
        "shortlist_size": len(shortlist),
        "avg_score": summary.get("avg_score"),
        "strategy_fit_density": summary.get("strategy_fit_density"),
        "strategy_seed": summary.get("strategy_seed"),
        "pinned": summary.get("pinned"),
        "top_symbols": [item.symbol for item in shortlist[:15]],
        "strategies_without_shortlist_fit_count": len(missing_strategies),
        "strategies_without_shortlist_fit": missing_strategies[:20],
        "summary": summary,
    }


def _print_report(report: dict[str, Any]) -> None:
    print("=== Shortlist coverage audit ===")
    if report.get("mode") == "live":
        print(f"mode: live ({report.get('config')})")
        print(
            f"funnel: raw={report.get('raw_tickers')} "
            f"gate_passed={report.get('gate_passed')} "
            f"light_pool={report.get('light_pool')}/{report.get('light_pool_limit')} "
            f"shortlist={report.get('shortlist_size')}"
        )
        print(
            f"quality: avg_score={report.get('avg_score')} "
            f"fit_density={report.get('strategy_fit_density')} "
            f"strategy_seed={report.get('strategy_seed')}"
        )
        print(f"top symbols: {', '.join(report.get('top_symbols') or [])}")
        print(
            "strategies without shortlist fit: "
            f"{report.get('strategies_without_shortlist_fit_count')}"
        )
        return

    latest = report.get("latest_shortlist") or {}
    build = report.get("latest_build") or {}
    print(f"telemetry shortlist rows: {report.get('telemetry_shortlist_rows')}")
    print(f"telemetry build rows: {report.get('telemetry_build_rows')}")
    print(
        f"latest funnel: gate_passed={latest.get('gate_passed')} "
        f"light_pool={latest.get('light_pool')}/{latest.get('light_pool_limit')} "
        f"size={latest.get('size')}"
    )
    if build:
        print(
            f"latest build: stage={build.get('stage')} source={build.get('source')} "
            f"fit_density={build.get('strategy_fit_density')}"
        )
    print(f"decision rows: {report.get('decision_rows')}")
    print(f"strategies with runs: {report.get('strategies_with_runs')}")
    print("top skip reasons:")
    for reason, count in report.get("top_skip_reasons") or []:
        print(f"  {reason}: {count}")
    print(
        "strategies without shortlist fit: "
        f"{report.get('strategies_without_shortlist_fit_count')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit shortlist funnel and strategy coverage")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--run-id", type=str, default="")
    parser.add_argument("--live", action="store_true", help="Build shortlist from live Binance REST")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args(argv)

    report: dict[str, Any]
    if args.live:
        report = asyncio.run(_live_shortlist_audit(args.config))
    else:
        settings = load_settings(args.config)
        run_dir = _latest_run_dir(settings.telemetry_dir, args.run_id or None)
        if run_dir is None:
            print("No telemetry run found; use --live or --run-id", file=sys.stderr)
            return 1
        analysis_dir = run_dir / "analysis"
        report = {
            "mode": "telemetry",
            "run_id": run_dir.name,
            "analysis_dir": str(analysis_dir),
            **_analyze_telemetry(analysis_dir),
        }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

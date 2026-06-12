#!/usr/bin/env python3
"""Independent audit of hunt tick rows — fresh REST client, no shared hunt state."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()

import logging

logging.basicConfig(level=logging.ERROR, force=True)

from hunt_watch.market_regime import active_params
from hunt_watch.paths import TICK_JSONL
from hunt_watch.signal_engine import cluster_fuel, confirm_dump, confirm_long

from hunt_core.domain.config import load_settings
from hunt_core.features.prepare import min_required_bars
from hunt_core.market import HuntCcxtClient


def _load_latest_rows(path: Path, *, max_ticks: int = 2) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows: list[dict[str, Any]] = []
    for ln in lines[-max_ticks:]:
        chunk = json.loads(ln)
        if isinstance(chunk, list):
            rows.extend(chunk)
        elif isinstance(chunk, dict):
            rows.append(chunk)
    return rows


def _candidate_filter(row: dict[str, Any], *, min_fuel: float) -> bool:
    if row.get("error"):
        return False
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    sf = float(dump.get("dump_fuel") or 0)
    lf = float(long_setup.get("long_fuel") or 0)
    return (
        bool(dump.get("confirmed"))
        or bool(long_setup.get("confirmed"))
        or sf >= min_fuel
        or lf >= min_fuel
    )


def _audit_row(
    hunt_row: dict[str, Any],
    indie_row: dict[str, Any],
) -> dict[str, Any]:
    sym = hunt_row.get("symbol")
    issues: list[str] = []
    checks: list[str] = []

    hp = float(hunt_row.get("price") or 0)
    ip = float(indie_row.get("price") or 0)
    if hp > 0 and ip > 0:
        drift = abs(hp - ip) / hp * 100
        checks.append(f"price_drift_pct={drift:.3f}")
        if drift > 0.35:
            issues.append(f"price_drift>{drift:.2f}% hunt={hp} indie={ip}")

    for side, fuel_key, score_key, setup_key in (
        ("short", "dump_fuel", "dump_score", "dump"),
        ("long", "long_fuel", "long_score", "long"),
    ):
        hunt_setup = hunt_row.get(setup_key) or {}
        indie_setup = indie_row.get(setup_key) or {}
        hf = float(hunt_setup.get(fuel_key) or 0)
        inf = float(indie_setup.get(fuel_key) or 0)
        if abs(hf - inf) > 0.6:
            issues.append(f"{side}_fuel_mismatch hunt={hf} indie={inf}")
        triggers = list(hunt_setup.get("triggers") or [])
        raw = float(hunt_setup.get(score_key) or 0)
        recomputed = cluster_fuel(triggers, raw_score=raw)
        if abs(recomputed - hf) > 0.6:
            issues.append(f"{side}_fuel_formula hunt={hf} recomputed={recomputed}")

        cal = active_params()
        tf = indie_row.get("timeframes") or {}
        lc = indie_row.get("lifecycle") or {}
        bias = str(lc.get("recommended_bias") or "")
        if side == "short":
            indie_conf, indie_hard = confirm_dump(
                indie_setup,
                tf,
                symbol=str(sym or ""),
                price=ip,
                market=indie_row.get("market") or {},
                cal=cal,
                lifecycle_bias=bias if bias in {"long", "short", "wait"} else "",
            )
            hunt_conf = bool(hunt_setup.get("confirmed"))
            hunt_hard = list(hunt_setup.get("confirm_hard") or [])
        else:
            indie_conf, indie_hard = confirm_long(
                indie_setup,
                tf,
                symbol=str(sym or ""),
                price=ip,
                cal=cal,
                lifecycle_bias=bias if bias in {"long", "short", "wait"} else "",
                lifecycle_phase=str(lc.get("phase") or ""),
            )
            hunt_conf = bool(hunt_setup.get("confirmed"))
            hunt_hard = list(hunt_setup.get("confirm_hard") or [])

        if indie_conf != hunt_conf:
            issues.append(
                f"{side}_confirmed_mismatch hunt={hunt_conf} indie={indie_conf} "
                f"hunt_hard={hunt_hard} indie_hard={indie_hard}"
            )
        elif hunt_conf:
            checks.append(f"{side}_confirmed_ok")

        # Closed-bar structural audit (independent of hunt's live_below_support)
        support = float((indie_setup.get("support_break_level") or 0) or 0)
        resistance = float((indie_setup.get("resistance_break_level") or 0) or 0)
        r5 = float((tf.get("5m_closed") or {}).get("close") or 0)
        r15 = float((tf.get("15m_closed") or {}).get("close") or 0)
        if side == "short" and support > 0:
            if r5 > 0 and r5 < support:
                checks.append("5m_closed_below_support=yes")
            elif r5 > 0:
                checks.append(f"5m_closed_below_support=no (close={r5:.6f} support={support:.6f})")
            if hunt_conf and r5 >= support:
                issues.append("confirmed_short_but_5m_close_not_below_support")

        if side == "long" and resistance > 0 and r5 > 0:
            if r5 > resistance:
                checks.append("5m_closed_above_resistance=yes")
            if hunt_conf and r5 <= resistance:
                issues.append("confirmed_long_but_5m_close_not_above_resistance")

    return {
        "symbol": sym,
        "ok": not issues,
        "issues": issues,
        "checks": checks,
        "hunt_phase": {
            "short": (hunt_row.get("dump") or {}).get("phase"),
            "long": (hunt_row.get("long") or {}).get("phase"),
        },
        "indie_phase": {
            "short": (indie_row.get("dump") or {}).get("phase"),
            "long": (indie_row.get("long") or {}).get("phase"),
        },
    }


async def _independent_snapshot(symbol: str, *, stagger_ms: int = 120) -> dict[str, Any]:
    import importlib.util

    watch_path = Path(__file__).resolve().parent / "watch.py"
    spec = importlib.util.spec_from_file_location("hunt_watch_script", watch_path)
    if spec is None or spec.loader is None:
        raise ImportError(watch_path)
    watch_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watch_mod)

    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    client = HuntCcxtClient.from_settings(settings)
    try:
        premium_all = await watch_mod._safe_fetch(client.fetch_premium_index_all()) or {}
        await asyncio.sleep(stagger_ms / 1000.0)
        funding_info_all = await watch_mod._safe_fetch(client.fetch_funding_info_all()) or {}
        await asyncio.sleep(stagger_ms / 1000.0)
        exchange_list = await watch_mod._safe_fetch(client.fetch_exchange_symbols()) or []
        exchange_by_sym = {r.symbol: r for r in exchange_list}
        await asyncio.sleep(stagger_ms / 1000.0)
        ticker_raw = await watch_mod._safe_fetch(client.fetch_ticker_24h()) or []
        ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
        btc_work_1h = None
        if symbol != "BTCUSDT":
            btc_df = await watch_mod._safe_fetch(
                client.fetch_klines_cached("BTCUSDT", "1h", limit=500)
            )
            if btc_df is not None and not btc_df.is_empty():
                btc_work_1h = watch_mod._prepare_frame(btc_df)
        return await watch_mod._snapshot_symbol(
            client,
            settings,
            minimums,
            symbol,
            watch_mode="both",
            prev_oi=None,
            premium_all=premium_all,
            funding_info_all=funding_info_all,
            btc_work_1h=btc_work_1h,
            exchange_by_sym=exchange_by_sym,
            ticker_by_sym=ticker_by_sym,
            ws_feed=None,
            spot_companion=None,
            stagger_klines_ms=stagger_ms,
        )
    finally:
        await client.close()


async def _main_async(
    *,
    symbols: list[str] | None,
    min_fuel: float,
    from_jsonl: bool,
) -> int:
    hunt_rows: list[dict[str, Any]] = []
    if from_jsonl:
        hunt_rows = [r for r in _load_latest_rows(TICK_JSONL) if _candidate_filter(r, min_fuel=min_fuel)]
        if symbols:
            symset = {s.upper() for s in symbols}
            hunt_rows = [r for r in hunt_rows if str(r.get("symbol", "")).upper() in symset]
    elif symbols:
        hunt_rows = [{"symbol": s.upper()} for s in symbols]
    else:
        hunt_rows = [r for r in _load_latest_rows(TICK_JSONL) if _candidate_filter(r, min_fuel=min_fuel)]

    if not hunt_rows:
        print(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "reports": [],
                    "note": "no_candidates",
                }
            )
        )
        return 0

    reports: list[dict[str, Any]] = []
    for hunt_row in hunt_rows:
        sym = str(hunt_row.get("symbol") or "").upper()
        if not sym:
            continue
        print(f"auditing {sym} …", file=sys.stderr, flush=True)
        indie = await _independent_snapshot(sym)
        if indie.get("error"):
            reports.append({"symbol": sym, "ok": False, "issues": [f"indie_error:{indie['error']}"]})
            continue
        reports.append(_audit_row(hunt_row if hunt_row.get("dump") else indie, indie))

    print(json.dumps({"ts": datetime.now(UTC).isoformat(), "reports": reports}, indent=2))
    bad = sum(1 for r in reports if not r.get("ok"))
    return 1 if bad else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent hunt setup audit")
    parser.add_argument("--symbols", nargs="*", help="Explicit symbols (skip jsonl row match)")
    parser.add_argument("--min-fuel", type=float, default=40.0)
    parser.add_argument("--no-jsonl", action="store_true", help="Only use --symbols")
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            _main_async(
                symbols=[s.upper() for s in args.symbols] if args.symbols else None,
                min_fuel=args.min_fuel,
                from_jsonl=not args.no_jsonl,
            )
        )
    )


if __name__ == "__main__":
    main()

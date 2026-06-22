"""Expansion Engine operator health check — synthetic + optional live."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any


def _config_summary() -> dict[str, Any]:
    from hunt_core._dev.expansion_lab.config import load_expansion_config

    cfg = load_expansion_config()
    return {
        "enabled": cfg.enabled,
        "watch_stamp": cfg.watch_stamp,
        "watch_stamp_tiers": sorted(cfg.watch_stamp_tiers),
        "tg_pinned": cfg.tg_pinned_alerts,
        "tg_universe": cfg.tg_universe_scan,
        "universe_interval_s": cfg.tg_universe_interval_s,
        "history_persist": cfg.history_persist,
        "runtime_save_interval_s": cfg.runtime_save_interval_s,
    }


def _ledger_summary() -> dict[str, Any]:
    from hunt_core._dev.expansion_lab.learning import (
        load_expansion_outcomes,
        summarize_outcomes,
    )
    from hunt_core._dev.expansion_lab.learning.review import pending_review_horizons

    records = load_expansion_outcomes()
    summary = summarize_outcomes(records)
    pending = sum(1 for r in records if pending_review_horizons(r))
    return {"records": len(records), "pending_reviews": pending, **summary}


def _runtime_summary() -> dict[str, Any]:
    from hunt_core.paths import (
        EXPANSION_ALERT_STATE,
        EXPANSION_CALIBRATION_JSON,
        EXPANSION_RUNTIME_STATE_JSON,
        EXPANSION_SCAN_JSONL,
    )

    fsm_n = 0
    hist_n = 0
    if EXPANSION_RUNTIME_STATE_JSON.is_file():
        try:
            raw = json.loads(EXPANSION_RUNTIME_STATE_JSON.read_text(encoding="utf-8"))
            fsm_n = len(raw.get("fsm") or {})
            hist_n = len(raw.get("history") or {})
        except (OSError, json.JSONDecodeError):
            pass
    scan_lines = 0
    if EXPANSION_SCAN_JSONL.is_file():
        try:
            scan_lines = sum(1 for ln in EXPANSION_SCAN_JSONL.read_text(encoding="utf-8").splitlines() if ln.strip())
        except OSError:
            pass
    alert_syms = 0
    if EXPANSION_ALERT_STATE.is_file():
        try:
            raw = json.loads(EXPANSION_ALERT_STATE.read_text(encoding="utf-8"))
            alert_syms = len((raw.get("sent") or {}))
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "fsm_symbols": fsm_n,
        "history_symbols": hist_n,
        "scan_jsonl_lines": scan_lines,
        "alert_cooldown_symbols": alert_syms,
        "calibration": EXPANSION_CALIBRATION_JSON.is_file(),
    }


def _cache_summary() -> dict[str, Any]:
    from hunt_core.runtime.expansion_universe_scan import collect_universe_rows

    rows = collect_universe_rows()
    stamped = sum(1 for r in rows.values() if isinstance(r.get("expansion"), dict))
    return {"cached_rows": len(rows), "stamped_expansion": stamped}


async def _live_probe(symbols: tuple[str, ...]) -> list[dict[str, Any]]:
    from hunt_core.bootstrap import bootstrap

    bootstrap()
    from hunt_core.runtime.expansion_probe import probe_symbol_expansion

    out: list[dict[str, Any]] = []
    for sym in symbols:
        row = await probe_symbol_expansion(sym, stagger_ms=200)
        exp = row.get("expansion") if isinstance(row.get("expansion"), dict) else {}
        out.append(
            {
                "symbol": row.get("symbol"),
                "error": row.get("error"),
                "state": exp.get("state"),
                "dominant": exp.get("dominant"),
                "expansion_score": exp.get("expansion_score"),
                "trigger_probability": exp.get("trigger_probability"),
                "opportunity_score": (exp.get("meta") or {}).get("opportunity_score"),
            }
        )
    return out


def _run_unit_checks() -> bool:
    from hunt_core._dev import check_expansion

    return check_expansion.main() == 0


async def _main_async(*, live: bool, symbols: tuple[str, ...], skip_unit: bool) -> int:
    fails: list[str] = []

    print("=== Expansion Health ===")
    print("config:", json.dumps(_config_summary(), ensure_ascii=False))
    print("ledger:", json.dumps(_ledger_summary(), ensure_ascii=False))
    print("runtime:", json.dumps(_runtime_summary(), ensure_ascii=False))
    print("cache:", json.dumps(_cache_summary(), ensure_ascii=False))

    cfg = _config_summary()
    if not cfg.get("enabled"):
        print("WARN: expansion disabled in config", file=sys.stderr)

    if not skip_unit:
        print("\n--- unit checks ---")
        if not _run_unit_checks():
            fails.append("unit checks failed")
        else:
            print("unit: OK")

    if live:
        print("\n--- live probe ---")
        probes = await _live_probe(symbols)
        print(json.dumps(probes, ensure_ascii=False, indent=2))
        for p in probes:
            if p.get("error"):
                fails.append(f"live {p.get('symbol')}: {p['error']}")
            elif p.get("expansion_score") is None:
                fails.append(f"live {p.get('symbol')}: missing expansion")

    cache = _cache_summary()
    if cache["cached_rows"] == 0 and not live:
        print("\nNOTE: tick cache empty — run watch or use --live for network probe")

    if fails:
        for f in fails:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("\nOK: expansion health")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Expansion Engine health check")
    parser.add_argument("--live", action="store_true", help="Live CCXT probe (BTC + alt)")
    parser.add_argument("--symbols", nargs="*", default=["BTCUSDT", "SOLUSDT"])
    parser.add_argument("--skip-unit", action="store_true", help="Skip synthetic unit checks")
    args = parser.parse_args(argv)
    symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
    return asyncio.run(_main_async(live=args.live, symbols=symbols, skip_unit=args.skip_unit))


if __name__ == "__main__":
    raise SystemExit(main())

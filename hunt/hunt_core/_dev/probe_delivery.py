"""Canonical delivery gate probe — use instead of ad-hoc inline scripts.

Run from repo root:
  python -m hunt_core._dev.probe_delivery BEATUSDT ALLOUSDT
Or from hunt/ (bootstrap silences venv prefix warnings):
  ../.venv/bin/python -m hunt_core._dev.probe_delivery BEATUSDT

Default reads last watch tick (LastTickStore / TICK_JSONL) — zero REST.
Use --live for on-demand REST probe (fast tier).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

from hunt_core.bootstrap import bootstrap

bootstrap()


def _evaluate_from_row(
    row: dict[str, Any],
    sym: str,
    *,
    live: bool,
    recompute: bool = False,
    direction: str = "short",
) -> dict[str, Any]:
    if recompute:
        from hunt_core._dev.replay_row import delivery_replay_report, recompute_tick_row

        if not row.get("recomputed"):
            row = recompute_tick_row(row)
        report = delivery_replay_report(row, direction=direction, recompute=False)
        report["source"] = "recomputed_tick"
        return report

    from hunt_core.runtime.query_service import build_query_result
    from hunt_core.track.tracker import (
        load_tracker_state,
        recent_stop_hit_cooldown,
        signal_confirm_announced,
        symbol_repeat_loser_blocked,
    )
    from hunt_core import clock

    dump = row.get("dump") or {}
    lc = row.get("lifecycle") or {}
    st = load_tracker_state()
    now = clock.now_utc()
    from_store = not live and str(row.get("tick_path") or "") not in {"", "live_rest"}
    query = build_query_result(
        row,
        sym,
        source="live_rest" if live else str(row.get("tick_path") or "tick_store"),
        from_store=from_store or row.get("tick_path") in {
            "hot_ws",
            "hot_bootstrap",
            "hot_delta",
            "hot_carry",
        },
        age_s=None,
    )
    focus = query.focus()
    out: dict[str, Any] = {
        "symbol": sym.upper(),
        "source": query.source,
        "confirmed": focus.confirmed,
        "phase": (row.get("dump") or {}).get("phase"),
        "lc_phase": lc.get("phase"),
        "score": dump.get("dump_score"),
        "fuel": dump.get("dump_fuel"),
        "fall_pct": lc.get("fall_from_high_pct"),
        "confirm_hard": (dump.get("confirm_hard") or [])[:8],
        "focus_direction": query.focus_direction,
        "formation_code": focus.formation.code,
        "would_deliver": focus.would_deliver,
        "tg_announced": signal_confirm_announced(st, symbol=sym, direction="short"),
        "post_sl_cooldown": recent_stop_hit_cooldown(
            st, symbol=sym, direction="short", now=now
        ),
    }
    if row.get("error"):
        out["error"] = row["error"]
        return out
    out["repeat_loser"] = symbol_repeat_loser_blocked(st, symbol=sym, now=now)
    if focus.confirmed:
        gate = focus.delivery_gate
        out["delivery_ok"] = focus.would_deliver
        out["gate_code"] = gate.code if gate else None
        out["gate_message"] = gate.message if gate else None
        out["tier"] = focus.delivery_tier
        out["delivery_lane"] = "fast" if query.from_store else "full"
    out["blocker_codes"] = [b.code for b in focus.blockers if not b.ok]
    return out


async def _probe_live(
    sym: str,
    *,
    client: Any,
    batch_cache: Any,
    ticker_by_sym: dict[str, dict[str, Any]],
    fast: bool,
    stagger_ms: int,
) -> tuple[dict[str, Any], float]:
    from hunt_core.runtime.symbol_probe import probe_symbol_signal

    t0 = time.monotonic()
    row = await probe_symbol_signal(
        sym,
        auto_watchlist=False,
        stagger_ms=stagger_ms,
        probe_kind="delivery" if fast else "signal",
        client=client,
        batch_cache=batch_cache if fast else None,
        ticker_by_sym=ticker_by_sym,
        tier="fast" if fast else "full",
    )
    elapsed = round(time.monotonic() - t0, 2)
    return row, elapsed


async def _probe_one(
    sym: str,
    *,
    live: bool,
    client: Any | None,
    batch_cache: Any,
    ticker_by_sym: dict[str, dict[str, Any]] | None,
    fast: bool,
    stagger_ms: int,
    recompute: bool = False,
    jsonl_path: Any | None = None,
    direction: str = "short",
) -> dict[str, Any]:
    if not live:
        from hunt_core.runtime.tick_state import last_tick_store

        t0 = time.monotonic()
        store = last_tick_store()
        row = store.get(sym)
        if row is None and jsonl_path is not None:
            row = store.tail_jsonl(sym, path=jsonl_path)
        if row is None:
            row = store.resolve(sym)
        elapsed = round(time.monotonic() - t0, 4)
        if row is None:
            return {
                "symbol": sym.upper(),
                "source": "missing",
                "error": "no_tick_row — run watch or use --live / --jsonl",
                "probe_s": elapsed,
            }
        out = _evaluate_from_row(
            row, sym, live=False, recompute=recompute, direction=direction
        )
        out["probe_s"] = elapsed
        out["row_ts"] = row.get("ts")
        return out
    if client is None or ticker_by_sym is None:
        raise RuntimeError("live probe requires shared client")
    row, elapsed = await _probe_live(
        sym,
        client=client,
        batch_cache=batch_cache,
        ticker_by_sym=ticker_by_sym,
        fast=fast,
        stagger_ms=stagger_ms,
    )
    out = _evaluate_from_row(row, sym, live=True, recompute=recompute, direction=direction)
    out["probe_s"] = elapsed
    return out


async def _run(
    symbols: list[str],
    *,
    stagger_ms: int,
    fast: bool,
    live: bool,
    recompute: bool = False,
    jsonl_path: Any | None = None,
    direction: str = "short",
) -> int:
    client: Any | None = None
    batch_cache: Any = None
    ticker_by_sym: dict[str, dict[str, Any]] | None = None
    if live:
        from hunt_core.data.collect import TickBatchCache, safe_fetch
        from hunt_core.domain.config import load_settings
        from hunt_core.market.factory import create_hunt_market_plane_from_settings

        settings = load_settings()
        plane = await create_hunt_market_plane_from_settings(settings)
        client = plane.client
        batch_cache = TickBatchCache()
        await client.load_markets()
        ticker_raw = await safe_fetch(client.fetch_ticker_24h(), context="ticker_24h") or []
        ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
    try:
        for sym in symbols:
            try:
                result = await _probe_one(
                    sym,
                    live=live,
                    client=client,
                    batch_cache=batch_cache,
                    ticker_by_sym=ticker_by_sym,
                    fast=fast,
                    stagger_ms=stagger_ms,
                    recompute=recompute,
                    jsonl_path=jsonl_path,
                    direction=direction,
                )
            except Exception as exc:
                print(f"{sym}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            print(f"\n=== {result['symbol']} ({result.get('probe_s', '?')}s) ===")
            for k, v in result.items():
                if k != "symbol":
                    print(f"  {k}: {v}")
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe hunt delivery gates for symbols")
    parser.add_argument("symbols", nargs="+", help="e.g. BEATUSDT ALLOUSDT")
    parser.add_argument(
        "--live",
        action="store_true",
        help="On-demand REST probe (default: read last watch tick, zero REST)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Full /signal-depth probe with --live (slow)",
    )
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="Re-score dump/long from stored tick using current code",
    )
    parser.add_argument(
        "--jsonl",
        type=str,
        default="",
        help="Tick JSONL path (default: hunt/data/dump_minute_watch.jsonl)",
    )
    parser.add_argument(
        "--direction",
        choices=("short", "long"),
        default="short",
        help="Delivery direction to probe (default: short)",
    )
    parser.add_argument("--stagger-ms", type=int, default=0, help="Kline stagger (full live only)")
    args = parser.parse_args(argv)
    syms = [s.upper() for s in args.symbols]
    jsonl_path = None
    if args.jsonl:
        from pathlib import Path

        jsonl_path = Path(args.jsonl)
    return asyncio.run(
        _run(
            syms,
            stagger_ms=args.stagger_ms,
            fast=not args.full,
            live=args.live,
            recompute=args.recompute,
            jsonl_path=jsonl_path,
            direction=args.direction,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

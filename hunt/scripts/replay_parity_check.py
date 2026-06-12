#!/usr/bin/env python3
"""Parity: jsonl_replay confirm vs signal_events confirmed (H-B cutover gate)."""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter

from hunt_core.bootstrap import bootstrap

bootstrap()

from hunt_core.data.store import LakeStore
from hunt_core.paths import DATA, SIGNAL_EVENTS
from hunt_watch.jsonl_replay import load_tick_rows, replay_row


def _confirmed_events(limit: int) -> list[dict]:
    rows: list[dict] = []
    if not SIGNAL_EVENTS.exists():
        return rows
    for line in SIGNAL_EVENTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "confirmed":
            rows.append(ev)
    random.shuffle(rows)
    return rows[:limit]


def _ticks_from_lake(symbols: set[str], *, limit_per_sym: int = 500) -> dict[str, list[dict]]:
    lake = LakeStore()
    by_sym: dict[str, list[dict]] = {s: [] for s in symbols}
    try:
        if lake.count() == 0:
            return by_sym
        for sym in symbols:
            for row in lake.iter_ticks(symbol=sym, limit=limit_per_sym):
                by_sym.setdefault(sym, []).append(row)
    finally:
        lake.close()
    return by_sym


def _ticks_from_jsonl(*, max_lines: int) -> dict[str, list[dict]]:
    tick_paths = sorted(DATA.glob("dump_minute_watch*.jsonl"))
    ticks = load_tick_rows(paths=tick_paths, max_lines=max_lines, strict_closed=False)
    by_sym: dict[str, list[dict]] = {}
    for row in ticks:
        sym = str(row.get("symbol") or "")
        by_sym.setdefault(sym, []).append(row)
    return by_sym


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sample", type=int, default=30)
    p.add_argument("--tick-limit", type=int, default=12000, help="JSONL tail when --no-lake")
    p.add_argument("--no-lake", action="store_true", help="Use JSONL tail instead of SQLite lake")
    p.add_argument("--lake-per-symbol", type=int, default=800)
    args = p.parse_args()
    events = _confirmed_events(args.sample)
    if not events:
        print("no confirmed events to check")
        return 1
    syms = {str(ev.get("symbol") or "") for ev in events}
    by_sym = (
        _ticks_from_jsonl(max_lines=args.tick_limit)
        if args.no_lake
        else _ticks_from_lake(syms, limit_per_sym=args.lake_per_symbol)
    )
    match = 0
    mismatch = 0
    reasons: Counter[str] = Counter()
    for ev in events:
        sym = str(ev.get("symbol") or "")
        direction = str(ev.get("direction") or "short")
        candidates = by_sym.get(sym) or []
        found = False
        for row in reversed(candidates):
            setup = (row.get("dump") if direction == "short" else row.get("long")) or {}
            if not bool(setup.get("confirmed")):
                continue
            rep = replay_row(row, direction=direction, recompute_lifecycle=False)
            if rep.confirmed:
                match += 1
            else:
                mismatch += 1
                reasons[str(rep.gate_code or "replay_not_confirmed")] += 1
            found = True
            break
        if not found:
            mismatch += 1
            reasons["no_tick_match"] += 1
    report = {
        "source": "jsonl" if args.no_lake else "lake",
        "sampled": len(events),
        "match": match,
        "mismatch": mismatch,
        "reasons": dict(reasons),
        "parity_rate": round(match / len(events), 3) if events else 0,
    }
    print(json.dumps(report, indent=2))
    return 0 if match >= max(1, len(events) // 3) else 1


if __name__ == "__main__":
    raise SystemExit(main())

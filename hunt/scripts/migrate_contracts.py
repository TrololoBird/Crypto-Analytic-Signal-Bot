#!/usr/bin/env python3
"""Normalize historical hunt JSONL to stable contracts (dedupe positioning, backfill phase)."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from hunt_core.bootstrap import bootstrap

bootstrap()

from hunt_core.contracts import normalize_tick_row
from hunt_core.paths import (
    BACKTEST_OUTCOMES,
    BACKTEST_OUTCOMES_ENRICHED,
    DATA,
    TICK_JSONL,
)
from hunt_research.labels import rebuild_unified


def _migrate_jsonl(path: Path, *, dry_run: bool) -> dict[str, int]:
    if not path.exists():
        return {"rows": 0, "changed": 0}
    lines = path.read_text(encoding="utf-8").splitlines()
    changed = 0
    out_lines: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        new_row = dict(row)
        if path.name.startswith("dump_minute_watch"):
            new_row = normalize_tick_row(row)
            if new_row != row:
                changed += 1
        elif "lifecycle_phase" not in new_row or not new_row.get("lifecycle_phase"):
            phase = new_row.get("entry_lifecycle_phase") or "unknown"
            new_row["lifecycle_phase"] = phase
            changed += 1
        out_lines.append(json.dumps(new_row, separators=(",", ":")))
    if not dry_run and changed:
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
    return {"rows": len(out_lines), "changed": changed}


def main() -> None:
    p = argparse.ArgumentParser(description="Migrate hunt data to stable contracts")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--backtest-only", action="store_true", help="Only migrate outcome JSONL files")
    p.add_argument("--ticks-only", action="store_true", help="Only migrate tick JSONL files")
    p.add_argument("--rebuild-labels", action="store_true", help="Rebuild unified_labels.jsonl")
    p.add_argument("--import-lake", action="store_true", help="Import ticks to SQLite lake")
    p.add_argument("--lake-limit-per-file", type=int, default=0, help="Cap rows per tick file (0=all)")
    p.add_argument("--lake-force", action="store_true", help="Rebuild lake DB from scratch")
    args = p.parse_args()

    stats: dict[str, dict[str, int]] = {}
    if not args.backtest_only:
        for tick in sorted(DATA.glob("dump_minute_watch*.jsonl")):
            stats[str(tick.name)] = _migrate_jsonl(tick, dry_run=args.dry_run)
    if not args.ticks_only:
        for outcome in (BACKTEST_OUTCOMES, BACKTEST_OUTCOMES_ENRICHED):
            if outcome.exists():
                stats[outcome.name] = _migrate_jsonl(outcome, dry_run=args.dry_run)

    print(json.dumps({"migrate": stats}, indent=2))

    if args.rebuild_labels and not args.dry_run:
        rows = rebuild_unified()
        print(f"unified_labels: {len(rows)} rows")

    if args.import_lake and not args.dry_run:
        from hunt_core.data.store import import_ticks_to_lake

        limit = args.lake_limit_per_file or None
        lake_stats = import_ticks_to_lake(limit_per_file=limit, force=args.lake_force)
        print(json.dumps({"lake": lake_stats}, indent=2))


if __name__ == "__main__":
    main()

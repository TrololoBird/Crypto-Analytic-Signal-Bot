"""Rotate hunt tick JSONL — daily files, gzip archive, 14-day retention."""

from __future__ import annotations

import gzip
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hunt_watch.paths import DATA, TICK_JSONL

RETENTION_DAYS = 14


def rotate_hunt_ticks(*, retention_days: int = RETENTION_DAYS, dry_run: bool = False) -> dict[str, int]:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    daily = DATA / f"dump_minute_watch-{today}.jsonl"
    stats = {"appended_lines": 0, "archived": 0, "pruned": 0}

    if not TICK_JSONL.exists():
        return stats

    size = TICK_JSONL.stat().st_size
    if size < 1024:
        return stats

    if daily.exists():
        with TICK_JSONL.open(encoding="utf-8") as src, daily.open("a", encoding="utf-8") as dst:
            for line in src:
                dst.write(line)
                stats["appended_lines"] += 1
    else:
        if not dry_run:
            shutil.move(str(TICK_JSONL), str(daily))
        stats["archived"] = 1

    if not dry_run:
        TICK_JSONL.write_text("", encoding="utf-8")

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    for path in sorted(DATA.glob("dump_minute_watch-*.jsonl")):
        stem = path.stem.replace("dump_minute_watch-", "")
        try:
            day = datetime.strptime(stem, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            continue
        if day < cutoff:
            gz = path.with_suffix(path.suffix + ".gz")
            if not dry_run:
                with path.open("rb") as f_in, gzip.open(gz, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                path.unlink()
            stats["pruned"] += 1

    return stats

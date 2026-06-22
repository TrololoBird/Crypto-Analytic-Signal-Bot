"""Counterfactual replay from outcome ledger geometry (I1 / phase 6)."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hunt_core.paths import DATA
from hunt_core.track.outcome_ledger import LEDGER_PATH


def load_ledger_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def counterfactual_horizon_stats(
    rows: list[dict],
    *,
    horizon_hours: float = 4.0,
) -> dict[str, int | float]:
    """Blocked rows with geometry — placeholder for full price-path replay."""
    blocked = [
        r
        for r in rows
        if r.get("counterfactual") or (not r.get("delivered") and r.get("event") != "delivered")
    ]
    with_geo = [r for r in blocked if r.get("stop_loss") and r.get("tp1")]
    recent = 0
    cutoff = datetime.now(UTC) - timedelta(hours=horizon_hours)
    for row in with_geo:
        ts = _parse_ts(row.get("ts"))
        if ts and ts >= cutoff:
            recent += 1
    return {
        "blocked_with_geometry": len(with_geo),
        "blocked_recent_with_geometry": recent,
        "horizon_hours": horizon_hours,
    }


def summarize(path: Path) -> dict[str, int | float]:
    rows = load_ledger_rows(path)
    blocked = [r for r in rows if r.get("counterfactual") or not r.get("delivered")]
    with_geo = [r for r in blocked if r.get("stop_loss") and r.get("tp1")]
    stats: dict[str, int | float] = {
        "total_rows": len(rows),
        "blocked_rows": len(blocked),
        "blocked_with_geometry": len(with_geo),
        "geometry_coverage_pct": round(100.0 * len(with_geo) / max(1, len(blocked)), 1),
    }
    stats.update(counterfactual_horizon_stats(rows))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ledger counterfactual geometry audit")
    parser.add_argument("--ledger", default=str(LEDGER_PATH))
    parser.add_argument("--lab", action="store_true", help="Use lab ledger path")
    args = parser.parse_args(argv)
    path = Path(args.ledger)
    if args.lab:
        path = DATA / "hunt_lab_outcome_ledger.jsonl"
    stats = summarize(path)
    print(json.dumps(stats, indent=2))
    if stats["blocked_rows"] and stats["blocked_with_geometry"] < stats["blocked_rows"]:
        print("WARN: some blocked rows missing geometry — I1 incomplete")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

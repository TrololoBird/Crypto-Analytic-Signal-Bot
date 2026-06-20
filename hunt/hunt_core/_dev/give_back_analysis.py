"""Offline give-back analysis from closed signal outcomes (Phase 7 / #34)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hunt_core.paths import SIGNAL_HISTORY


def load_outcome_rows(path: Path = SIGNAL_HISTORY) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def summarize_give_back(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """MFE vs exit PnL — how much profit was given back before close."""
    data = rows if rows is not None else load_outcome_rows()
    stats: dict[str, Any] = {"n": 0, "avg_give_back_pct": 0.0, "by_reason": {}}
    give_backs: list[float] = []
    for row in data:
        mfe = row.get("mfe_pct")
        pnl = row.get("pnl_pct")
        if mfe is None or pnl is None:
            continue
        try:
            mfe_f = float(mfe)
            pnl_f = float(pnl)
        except (TypeError, ValueError):
            continue
        if mfe_f <= 0:
            continue
        gb = max(0.0, mfe_f - pnl_f)
        give_backs.append(gb)
        reason = str(row.get("reason") or row.get("close_reason") or "unknown")
        bucket = stats["by_reason"].setdefault(reason, {"n": 0, "avg_give_back_pct": 0.0})
        bucket["n"] += 1
        bucket["avg_give_back_pct"] = round(
            (bucket["avg_give_back_pct"] * (bucket["n"] - 1) + gb) / bucket["n"],
            3,
        )
    stats["n"] = len(give_backs)
    if give_backs:
        stats["avg_give_back_pct"] = round(sum(give_backs) / len(give_backs), 3)
    return stats


if __name__ == "__main__":
    import pprint

    pprint.pp(summarize_give_back())

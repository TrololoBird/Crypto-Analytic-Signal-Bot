#!/usr/bin/env python3
"""Print North Star KPI snapshot for H-B (hold-to-target slices)."""

from __future__ import annotations

import json

from hunt_core.bootstrap import bootstrap

bootstrap()

from hunt_core.gate.edge_policy import EdgePolicyConfig, long_tg_allowed
from hunt_research.labels import load_unified, slice_stats
from hunt_watch.calibration import compute_backtest_rates, early_exit_verdict


def main() -> int:
    labels = load_unified(rebuild=False)
    stats = slice_stats(labels)
    long_ok, long_reason = long_tg_allowed(EdgePolicyConfig.from_env())
    payload = {
        "north_star": {
            "short_dump_active": stats.get("short|dump_active"),
            "short_all": _agg(stats, "short"),
            "long_all": _agg(stats, "long"),
        },
        "long_tg": {"allowed": long_ok, "reason": long_reason},
        "backtest": compute_backtest_rates(),
        "early_exit": early_exit_verdict(),
        "slices": stats,
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


def _agg(stats: dict, direction: str) -> dict | None:
    keys = [k for k in stats if k.startswith(f"{direction}|")]
    if not keys:
        return None
    n = sum(stats[k]["n"] for k in keys)
    if n == 0:
        return None
    sl = sum((stats[k]["sl_rate"] or 0) * stats[k]["n"] for k in keys) / n
    tp = sum((stats[k]["tp1_plus_rate"] or 0) * stats[k]["n"] for k in keys) / n
    return {"n": n, "sl_rate": round(sl, 3), "tp1_plus_rate": round(tp, 3)}


if __name__ == "__main__":
    raise SystemExit(main())

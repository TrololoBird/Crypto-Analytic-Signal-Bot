#!/usr/bin/env python3
"""Hunter health rollup — WS/proxy/funnel North Star snapshot."""

from __future__ import annotations

import json
import sys

from hunt_core.bootstrap import bootstrap

bootstrap()

from hunt_core.gate.edge_policy import EdgePolicyConfig, long_tg_allowed
from hunt_core.paths import SIGNAL_EVENTS, SIGNAL_STATE, TICK_JSONL
from hunt_research.labels import load_unified, slice_stats


def main() -> int:
    state_ok = SIGNAL_STATE.exists()
    tick_mb = TICK_JSONL.stat().st_size / (1024 * 1024) if TICK_JSONL.exists() else 0
    events_n = 0
    confirmed_n = 0
    if SIGNAL_EVENTS.exists():
        for line in SIGNAL_EVENTS.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            events_n += 1
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "confirmed":
                confirmed_n += 1
    long_ok, long_reason = long_tg_allowed(EdgePolicyConfig.from_env())
    labels = load_unified(rebuild=False) if True else []
    stats = slice_stats(labels) if labels else {}
    report = {
        "signal_state": state_ok,
        "tick_jsonl_mb": round(tick_mb, 1),
        "signal_events": events_n,
        "confirmed_events": confirmed_n,
        "long_tg_allowed": long_ok,
        "long_tg_reason": long_reason,
        "label_slices": stats,
        "wide_mode": EdgePolicyConfig.from_env().wide_hunter,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Hunt outcomes report — score buckets x outcomes from tracker state.

Calibration input: confirm_min_score / fuel thresholds should follow this
table, not intuition. Requires close_reason/pnl_pct (recorded since
phase-hunt-v4-1); older closed signals without them are listed as unknown.

Usage:
    python hunt/scripts/outcomes_report.py
"""

from __future__ import annotations

import json
from collections import defaultdict

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.paths import SIGNAL_STATE

WIN_REASONS = {"tp1", "tp2"}
LOSS_REASONS = {"stop_hit", "bounce_invalidate", "trend_exhaustion", "reclaim_invalidation", "support_lost", "bias_flip", "lifecycle_stale", "opposite_signal"}


def _bucket(score: float) -> str:
    lo = int(score // 10) * 10
    return f"{lo}-{lo + 9}"


def main() -> int:
    raw = json.loads(SIGNAL_STATE.read_text(encoding="utf-8"))
    signals = raw.get("signals") or {}
    rows = [v for v in signals.values() if isinstance(v, dict)]
    closed = [r for r in rows if r.get("status") == "closed"]
    active = [r for r in rows if r.get("status") == "active"]
    print(f"signals: {len(rows)} total · {len(active)} active · {len(closed)} closed\n")

    buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    reasons: dict[str, int] = defaultdict(int)
    for r in closed:
        score = float(r.get("score") or 0)
        reason = str(r.get("close_reason") or "unknown")
        reasons[reason] += 1
        pnl = r.get("pnl_pct")
        kind = "win" if reason in WIN_REASONS else "loss" if reason in LOSS_REASONS else "unknown"
        buckets[_bucket(score)][kind].append(float(pnl) if pnl is not None else 0.0)

    print("close reasons:")
    for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {reason:22s} {n}")

    print("\nscore bucket | n | win | loss | unknown | win% (known) | avg pnl%")
    for bucket in sorted(buckets):
        b = buckets[bucket]
        w, l, u = len(b["win"]), len(b["loss"]), len(b["unknown"])
        known = w + l
        winrate = f"{w / known * 100:.0f}%" if known else "—"
        pnls = b["win"] + b["loss"] + b["unknown"]
        avg = sum(pnls) / len(pnls) if pnls else 0.0
        print(f"{bucket:>12} | {w + l + u} | {w} | {l} | {u} | {winrate:>6} | {avg:+.2f}")

    # Entry lifecycle phase × direction: which phases are worth trading.
    # entry_lifecycle_phase is immutable from open; lifecycle_phase mutates
    # per tick (followups) and is only the fallback for pre-fix rows.
    phased: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in closed:
        phase = str(r.get("entry_lifecycle_phase") or r.get("lifecycle_phase") or "?")
        direction = str(r.get("direction") or "?")
        reason = str(r.get("close_reason") or "unknown")
        kind = "win" if reason in WIN_REASONS else "loss" if reason in LOSS_REASONS else "unknown"
        pnl = r.get("pnl_pct")
        phased[(phase, direction)][kind].append(float(pnl) if pnl is not None else 0.0)

    print("\nentry phase × direction | n | win | loss | win% (known) | avg pnl%")
    for (phase, direction), b in sorted(phased.items()):
        w, l, u = len(b["win"]), len(b["loss"]), len(b["unknown"])
        known = w + l
        winrate = f"{w / known * 100:.0f}%" if known else "—"
        pnls = b["win"] + b["loss"] + b["unknown"]
        avg = sum(pnls) / len(pnls) if pnls else 0.0
        print(f"{phase:>22} {direction:>5} | {w + l + u} | {w} | {l} | {winrate:>6} | {avg:+.2f}")

    durs = [float(r.get("duration_min") or 0) for r in closed if r.get("duration_min")]
    if durs:
        durs.sort()
        print(f"\nduration_min: median {durs[len(durs) // 2]:.0f} · max {durs[-1]:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

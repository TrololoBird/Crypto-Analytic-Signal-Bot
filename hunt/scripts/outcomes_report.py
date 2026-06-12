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
# Hard losses (bias was wrong, price moved against us)
STOP_REASONS = {"stop_hit"}
# Soft exits — positive pnl = scratch_win, negative = thesis_fail
SOFT_REASONS = {"bounce_invalidate", "trend_exhaustion", "reclaim_invalidation", "support_lost",
                "bias_flip", "lifecycle_stale", "opposite_signal"}


def _thesis_outcome(reason: str, pnl: float | None, *, tp1_managed: bool = False) -> str:
    """Classify signal outcome honestly.

    tp_hit      — TP1 or TP2 reached (thesis fully validated)
    scratch_win — soft/managed exit but directionally correct (tp1 managed, or soft exit with pnl>0)
    stop_loss   — hard stop without TP1 (thesis failed at entry)
    thesis_fail — soft exit with negative/zero pnl (thesis wrong)
    unknown     — no close_reason recorded
    """
    if reason in WIN_REASONS:
        return "tp_hit"
    if reason in STOP_REASONS:
        # TP1 was already taken, trailing stop hit at breakeven — not a real thesis failure
        if tp1_managed:
            return "scratch_win"
        return "stop_loss"
    if reason in SOFT_REASONS:
        return "scratch_win" if (pnl is not None and pnl > 0) else "thesis_fail"
    return "unknown"


def _bucket(score: float) -> str:
    lo = int(score // 10) * 10
    return f"{lo}-{lo + 9}"


def main() -> int:
    raw = json.loads(SIGNAL_STATE.read_text(encoding="utf-8"))
    signals = raw.get("signals") or {}
    # Derive symbol/direction from key into each signal record
    rows = []
    for k, v in signals.items():
        if not isinstance(v, dict):
            continue
        sym, _, direction = k.partition(":")
        r = dict(v)
        r.setdefault("symbol", sym or None)
        r.setdefault("direction", direction or None)
        rows.append(r)
    active = [r for r in rows if r.get("status") == "active"]
    # closed_history accumulates ALL closes including repeat signals on the same key
    closed_history = raw.get("closed_history") or []
    closed_from_dict = [r for r in rows if r.get("status") == "closed"]
    # Merge: history wins for its entries (dedup by symbol:direction), dict fills gaps
    hist_keys = {(r.get("symbol") or "") + ":" + (r.get("direction") or "") for r in closed_history}
    closed = closed_history + [r for r in closed_from_dict if (r.get("symbol") or "") + ":" + (r.get("direction") or "") not in hist_keys]
    print(f"signals: {len(rows)} total · {len(active)} active · {len(closed)} closed (history={len(closed_history)})\n")

    buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    reasons: dict[str, int] = defaultdict(int)
    thesis_counts: dict[str, int] = defaultdict(int)
    for r in closed:
        score = float(r.get("score") or 0)
        reason = str(r.get("close_reason") or "unknown")
        reasons[reason] += 1
        pnl = r.get("pnl_pct")
        pnl_f = float(pnl) if pnl is not None else 0.0
        tp1_managed = bool(r.get("tp1_managed"))
        outcome = _thesis_outcome(reason, pnl, tp1_managed=tp1_managed)
        thesis_counts[outcome] += 1
        # Bucket uses tp_hit+scratch_win as "win", stop_loss+thesis_fail as "loss"
        kind = "win" if outcome in ("tp_hit", "scratch_win") else "loss" if outcome in ("stop_loss", "thesis_fail") else "unknown"
        buckets[_bucket(score)][kind].append(pnl_f)

    print("close reasons:")
    for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {reason:22s} {n}")

    # Thesis outcome summary
    total_known = sum(thesis_counts[k] for k in ("tp_hit", "scratch_win", "stop_loss", "thesis_fail"))
    tp = thesis_counts["tp_hit"]
    sw = thesis_counts["scratch_win"]
    sl = thesis_counts["stop_loss"]
    tf = thesis_counts["thesis_fail"]
    thesis_success = tp + sw
    success_rate = f"{thesis_success / total_known * 100:.0f}%" if total_known else "—"
    tp_rate = f"{tp / total_known * 100:.0f}%" if total_known else "—"
    print(f"\nthesis outcomes (n={total_known}):")
    print(f"  tp_hit={tp}  scratch_win={sw}  stop_loss={sl}  thesis_fail={tf}  unknown={thesis_counts['unknown']}")
    print(f"  thesis_success (tp+scratch): {thesis_success}/{total_known} = {success_rate}  |  tp_hit rate: {tp}/{total_known} = {tp_rate}")

    print("\nscore bucket | n | pos | neg | unknown | pos% (known) | avg pnl%")
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
    phased_thesis: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in closed:
        phase = str(r.get("entry_lifecycle_phase") or r.get("lifecycle_phase") or "?")
        direction = str(r.get("direction") or "?")
        reason = str(r.get("close_reason") or "unknown")
        pnl = r.get("pnl_pct")
        pnl_f = float(pnl) if pnl is not None else 0.0
        tp1_managed = bool(r.get("tp1_managed"))
        outcome = _thesis_outcome(reason, pnl, tp1_managed=tp1_managed)
        phased_thesis[(phase, direction)][outcome] += 1
        kind = "win" if outcome in ("tp_hit", "scratch_win") else "loss" if outcome in ("stop_loss", "thesis_fail") else "unknown"
        phased[(phase, direction)][kind].append(pnl_f)

    print("\nentry phase × direction | n | tp | sw | sl | tf | thesis% | avg pnl%")
    for (phase, direction), b in sorted(phased.items()):
        tc = phased_thesis[(phase, direction)]
        tp_n, sw_n, sl_n, tf_n = tc["tp_hit"], tc["scratch_win"], tc["stop_loss"], tc["thesis_fail"]
        total_n = tp_n + sw_n + sl_n + tf_n + tc["unknown"]
        known_n = tp_n + sw_n + sl_n + tf_n
        thesis_n = tp_n + sw_n
        thesis_pct = f"{thesis_n / known_n * 100:.0f}%" if known_n else "—"
        pnls = b["win"] + b["loss"] + b["unknown"]
        avg = sum(pnls) / len(pnls) if pnls else 0.0
        print(f"{phase:>22} {direction:>5} | {total_n} | {tp_n} | {sw_n} | {sl_n} | {tf_n} | {thesis_pct:>7} | {avg:+.2f}")

    # Fuel bucket — only meaningful once fuel is stored (signals opened after W19 fix)
    fuel_buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in closed:
        raw_fuel = r.get("fuel")
        if raw_fuel is None:
            continue
        fuel = int(float(raw_fuel))
        lo = (fuel // 16) * 16
        bkt = f"{lo}-{lo + 15}"
        reason = str(r.get("close_reason") or "unknown")
        pnl = r.get("pnl_pct")
        pnl_f = float(pnl) if pnl is not None else 0.0
        tp1_managed = bool(r.get("tp1_managed"))
        outcome = _thesis_outcome(reason, pnl, tp1_managed=tp1_managed)
        kind = "win" if outcome in ("tp_hit", "scratch_win") else "loss" if outcome in ("stop_loss", "thesis_fail") else "unknown"
        fuel_buckets[bkt][kind].append(pnl_f)
    if fuel_buckets:
        print("\nfuel bucket | n | pos | neg | pos% | avg pnl%")
        for bkt in sorted(fuel_buckets):
            b = fuel_buckets[bkt]
            w, l, u = len(b["win"]), len(b["loss"]), len(b["unknown"])
            known = w + l
            winrate = f"{w / known * 100:.0f}%" if known else "—"
            pnls = b["win"] + b["loss"] + b["unknown"]
            avg = sum(pnls) / len(pnls) if pnls else 0.0
            print(f"{bkt:>11} | {w + l + u} | {w} | {l} | {winrate:>5} | {avg:+.2f}")

    durs = [float(r.get("duration_min") or 0) for r in closed if r.get("duration_min")]
    if durs:
        durs.sort()
        print(f"\nduration_min: median {durs[len(durs) // 2]:.0f} · max {durs[-1]:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

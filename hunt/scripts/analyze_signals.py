#!/usr/bin/env python3
"""Forensic signal analysis — live closes, SL geometry, hold-to-target join, gate-edge slices.

Surfaces analysis errors (thesis inflation, polluted rows, TP1-BE false stops).

Usage:
    python hunt/scripts/analyze_signals.py
    python hunt/scripts/analyze_signals.py --json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.calibration import compute_backtest_rates, compute_gate_edge, early_exit_verdict
from hunt_watch.paths import (
    BACKTEST_OUTCOMES,
    BACKTEST_OUTCOMES_ENRICHED,
    GATE_EDGE_OUTCOMES,
    SIGNAL_HISTORY,
    SIGNAL_STATE,
)

WIN_REASONS = {"tp1", "tp2"}
STOP_REASONS = {"stop_hit"}
SOFT_REASONS = {
    "bounce_invalidate",
    "trend_exhaustion",
    "reclaim_invalidation",
    "support_lost",
    "bias_flip",
    "lifecycle_stale",
    "opposite_signal",
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def _thesis_outcome(reason: str, pnl: float | None, *, tp1_managed: bool = False) -> str:
    if reason in WIN_REASONS:
        return "tp_hit"
    if reason in STOP_REASONS:
        return "scratch_win" if tp1_managed else "stop_loss"
    if reason in SOFT_REASONS:
        return "scratch_win" if (pnl is not None and pnl > 0) else "thesis_fail"
    return "unknown"


def _sl_above_hi_pct(row: dict[str, Any]) -> float | None:
    hi, sl = row.get("entry_hi"), row.get("stop_loss")
    if hi is None or sl is None:
        return None
    hi_f, sl_f = float(hi), float(sl)
    if hi_f <= 0:
        return None
    return round((sl_f - hi_f) / hi_f * 100.0, 3)


def _is_polluted(row: dict[str, Any]) -> bool:
    return not row.get("opened_at") or row.get("fuel") is None


def _gate_edge_slices(rows: list[dict[str, Any]], *, min_n: int = 5) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = defaultdict(list)

    def add(key: str, outcome: str) -> None:
        groups[key].append(outcome)

    for r in rows:
        oc = str(r.get("bt_outcome") or "unknown")
        direction = str(r.get("direction") or "?")
        phase = str(r.get("lifecycle_phase") or "unknown")
        add(f"all:{direction}", oc)
        add(f"phase:{phase}:{direction}", oc)
        fuel = r.get("fuel")
        if fuel is not None:
            f = int(float(fuel))
            lo = (f // 16) * 16
            add(f"fuel:{lo}-{lo + 15}:{direction}", oc)

    out: list[dict[str, Any]] = []
    for key, outcomes in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        n = len(outcomes)
        if n < min_n:
            continue
        sl = sum(1 for o in outcomes if o == "sl_hit") / n
        tp = sum(1 for o in outcomes if o in ("tp1_hit", "tp2_hit")) / n
        out.append({"slice": key, "n": n, "sl_rate": round(sl, 3), "tp1_reach": round(tp, 3)})
    return out


def _join_backtest(
    live: dict[str, Any], backtest_rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    sym = live.get("symbol")
    opened = live.get("opened_at")
    if not sym or not opened:
        return None
    for bt in backtest_rows:
        if bt.get("symbol") != sym:
            continue
        if bt.get("source") != "signal_history":
            continue
        if str(bt.get("opened_at", ""))[:16] == str(opened)[:16]:
            return bt
    for bt in backtest_rows:
        if bt.get("symbol") == sym and bt.get("source") == "signal_history":
            return bt
    return None


def build_report() -> dict[str, Any]:
    history = _load_jsonl(SIGNAL_HISTORY)
    state = json.loads(SIGNAL_STATE.read_text(encoding="utf-8")) if SIGNAL_STATE.exists() else {}
    closed_history = state.get("closed_history") or []
    backtest = _load_jsonl(
        BACKTEST_OUTCOMES_ENRICHED if BACKTEST_OUTCOMES_ENRICHED.exists() else BACKTEST_OUTCOMES
    )
    gate_rows = _load_jsonl(GATE_EDGE_OUTCOMES)

    genuine = [r for r in history if not _is_polluted(r) and r.get("close_reason")]
    polluted = [r for r in history if _is_polluted(r)]

    signal_rows: list[dict[str, Any]] = []
    for r in genuine:
        reason = str(r.get("close_reason") or "")
        pnl = r.get("pnl_pct")
        pnl_f = float(pnl) if pnl is not None else None
        tp1_managed = bool(r.get("tp1_managed"))
        thesis = _thesis_outcome(reason, pnl_f, tp1_managed=tp1_managed)
        sl_pct = _sl_above_hi_pct(r)
        bt = _join_backtest(r, backtest)
        verdict = "ok"
        if reason == "stop_hit" and r.get("tp1_hit"):
            verdict = "false_stop_after_tp1"
        elif sl_pct is not None and sl_pct < 0.5 and reason == "stop_hit":
            verdict = "tight_sl_noise"
        elif (
            reason in SOFT_REASONS
            and bt
            and bt.get("bt_outcome") in ("tp2_hit", "tp1_hit")
            and (pnl_f or 0) < float(bt.get("bt_mfe_pct") or 0) * 0.5
        ):
            verdict = "early_exit_forfeited_tp"

        signal_rows.append(
            {
                "symbol": r.get("symbol"),
                "direction": r.get("direction"),
                "phase": r.get("entry_lifecycle_phase"),
                "close_reason": reason,
                "thesis": thesis,
                "pnl_pct": pnl_f,
                "fuel": r.get("fuel"),
                "score": r.get("score"),
                "tp1_hit": bool(r.get("tp1_hit")),
                "tp1_managed": tp1_managed,
                "sl_above_hi_pct": sl_pct,
                "original_stop_loss": r.get("original_stop_loss"),
                "mfe_pct": r.get("mfe_pct"),
                "hold_to_target": bt.get("bt_outcome") if bt else None,
                "forensic_verdict": verdict,
            }
        )

    thesis_counts = Counter(r["thesis"] for r in signal_rows)
    close_counts = Counter(r["close_reason"] for r in signal_rows)
    false_stops = [r for r in signal_rows if r["forensic_verdict"] == "false_stop_after_tp1"]

    return {
        "generated_from": {
            "signal_history": str(SIGNAL_HISTORY),
            "n_history": len(history),
            "n_genuine": len(genuine),
            "n_polluted": len(polluted),
            "n_closed_history": len(closed_history),
        },
        "live_summary": {
            "close_reason": dict(close_counts),
            "thesis": dict(thesis_counts),
            "tp_hit_rate": round(
                thesis_counts.get("tp_hit", 0) / len(signal_rows), 3
            )
            if signal_rows
            else None,
            "thesis_success_rate": round(
                (thesis_counts.get("tp_hit", 0) + thesis_counts.get("scratch_win", 0))
                / len(signal_rows),
                3,
            )
            if signal_rows
            else None,
            "false_stop_after_tp1": len(false_stops),
        },
        "signals": signal_rows,
        "polluted_symbols": Counter(r.get("symbol") for r in polluted),
        "early_exit_verdict": early_exit_verdict(),
        "backtest_truth": compute_backtest_rates(),
        "gate_edge_summary": compute_gate_edge(),
        "gate_edge_slices": _gate_edge_slices(gate_rows),
        "analysis_warnings": [
            "thesis_success mixes scratch_win with tp_hit — use tp_hit_rate + hold_to_target",
            "backtest_truth sl_hit_rate is mostly synthetic pump_history, not live confirm",
            "polluted rows (opened_at=None) must be excluded from live WR",
            "stop_hit with tp1_hit=true is usually BE-buffer too tight, not wrong fade thesis",
        ],
    }


def _print_text(report: dict[str, Any]) -> None:
    meta = report["generated_from"]
    print(
        f"signal forensic · history={meta['n_history']} "
        f"genuine={meta['n_genuine']} polluted={meta['n_polluted']}\n"
    )

    ls = report["live_summary"]
    print("close_reason:", ls["close_reason"])
    print("thesis:", ls["thesis"])
    print(
        f"tp_hit_rate={ls['tp_hit_rate']}  thesis_success={ls['thesis_success_rate']}  "
        f"false_stop_after_tp1={ls['false_stop_after_tp1']}\n"
    )

    print(f"{'symbol':12} {'close':16} {'thesis':12} {'pnl':>7} {'sl%+':>6} {'hold':>10} verdict")
    for r in report["signals"]:
        sl = f"{r['sl_above_hi_pct']:.2f}" if r["sl_above_hi_pct"] is not None else "?"
        pnl = f"{r['pnl_pct']:+.2f}" if r["pnl_pct"] is not None else "?"
        print(
            f"{r['symbol']:12} {r['close_reason']:16} {r['thesis']:12} {pnl:>7} "
            f"{sl:>6} {str(r['hold_to_target'] or '?'):>10} {r['forensic_verdict']}"
        )

    if report["polluted_symbols"]:
        print(f"\npolluted rows: {dict(report['polluted_symbols'])}")

    eev = report.get("early_exit_verdict") or {}
    if eev.get("summary"):
        print(f"\n{eev['summary']}")

    bt = report.get("backtest_truth") or {}
    print(
        f"\nbacktest truth ({bt.get('source')} n={bt.get('n_graded')}): "
        f"sl={bt.get('sl_hit_rate')} tp_reach={bt.get('tp1_reach_rate')}"
    )

    ge = report.get("gate_edge_summary") or {}
    by_dir = ge.get("by_direction") or {}
    for d, stats in by_dir.items():
        print(
            f"gate_edge {d}: n={stats.get('n')} sl={stats.get('sl_rate')} "
            f"tp1={stats.get('tp1_reach')} edge_pp={stats.get('edge_pp')}"
        )

    print("\ngate_edge slices (n≥5):")
    for row in report.get("gate_edge_slices") or []:
        print(
            f"  {row['slice']:40} n={row['n']:3} "
            f"sl={row['sl_rate']:.0%} tp1+={row['tp1_reach']:.0%}"
        )

    print("\nanalysis warnings:")
    for w in report.get("analysis_warnings") or []:
        print(f"  · {w}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Hunt signal forensic report")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()
    report = build_report()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

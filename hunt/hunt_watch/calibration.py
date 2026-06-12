"""Auto-calibration from closed_history and signal_history.jsonl.

Reads closed signal history and suggests threshold adjustments.
Never applies changes automatically — safe_to_apply flag only.
Guardrail: safe_to_apply=False when thesis_success < 70% or n < 20.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

WIN_REASONS = {"tp1", "tp2"}
STOP_REASONS = {"stop_hit"}
SOFT_REASONS = {
    "bounce_invalidate", "trend_exhaustion", "reclaim_invalidation",
    "support_lost", "bias_flip", "lifecycle_stale", "opposite_signal",
}

FUEL_BUCKET_WIDTH = 16
SCORE_BUCKET_WIDTH = 20
MIN_N = 20
SAFE_N = 30
THESIS_SUCCESS_FLOOR = 0.70
CLEAR_DELTA_WR = 0.05  # >5% delta to suggest
MIN_BUCKET_N = 10
# Backtest (hold-to-target) is the UNBIASED truth source. The live thesis metric
# is inflated by the tracker's early-exit policy (closes before SL → never logs a
# loss). Loosening thresholds is blocked when the backtest SL rate is too high.
BACKTEST_SL_GATE = 0.30  # block safe_to_apply when hold-to-target sl_hit rate > 30%
MIN_BACKTEST_N = 30  # need this many graded outcomes before the SL gate has teeth


def compute_backtest_rates(path: Path | None = None) -> dict[str, Any]:
    """Hold-to-target outcome rates from backtest_outcomes.jsonl — the unbiased truth.

    Live close stats never record a stop (early-exit policy), so calibration must
    anchor its safety gate on these rates instead.
    """
    from hunt_watch.paths import BACKTEST_OUTCOMES, BACKTEST_OUTCOMES_ENRICHED

    # Prefer the ATR-enriched grade (realistic vol-based levels) when present.
    if path is None:
        path = BACKTEST_OUTCOMES_ENRICHED if BACKTEST_OUTCOMES_ENRICHED.exists() else BACKTEST_OUTCOMES
    rows = _load_history_jsonl(path)
    counts: dict[str, int] = defaultdict(int)
    graded = 0
    for r in rows:
        oc = str(r.get("bt_outcome") or "unknown")
        counts[oc] += 1
        if oc in ("tp1_hit", "tp2_hit", "sl_hit", "timeout"):
            graded += 1
    sl_hit = counts.get("sl_hit", 0)
    tp_reach = counts.get("tp1_hit", 0) + counts.get("tp2_hit", 0)
    sl_rate = sl_hit / graded if graded else None
    tp1_reach_rate = tp_reach / graded if graded else None
    return {
        "n_graded": graded,
        "counts": dict(counts),
        "sl_hit_rate": round(sl_rate, 3) if sl_rate is not None else None,
        "tp1_reach_rate": round(tp1_reach_rate, 3) if tp1_reach_rate is not None else None,
        "source": path.name,
    }
MIN_PHASE_N = 15
PHASE_WIN_FLOOR = 0.55


def _thesis_outcome(reason: str, pnl: float | None, *, tp1_managed: bool = False) -> str:
    if reason in WIN_REASONS:
        return "tp_hit"
    if reason in STOP_REASONS:
        return "scratch_win" if tp1_managed else "stop_loss"
    if reason in SOFT_REASONS:
        return "scratch_win" if (pnl is not None and pnl > 0) else "thesis_fail"
    return "unknown"


def compute_gate_edge(path: Path | None = None) -> dict[str, Any]:
    """Confirmed-gate SL/TP rates per direction vs the raw-fade baseline.

    Reads gate_edge_outcomes.jsonl (written by scripts/gate_edge.py). Proves how
    much the confirm gate filters losers: confirmed SL rate vs ~52% raw baseline.
    """
    from hunt_watch.paths import GATE_EDGE_OUTCOMES

    rows = _load_history_jsonl(path or GATE_EDGE_OUTCOMES)
    by_dir: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        d = str(r.get("direction") or "?")
        oc = str(r.get("bt_outcome") or "no_data")
        if oc in ("tp1_hit", "tp2_hit", "sl_hit", "timeout"):
            by_dir[d][oc] += 1
    out: dict[str, Any] = {"raw_baseline_sl": 0.52, "by_direction": {}}
    for d, c in by_dir.items():
        graded = sum(c.values())
        if not graded:
            continue
        sl = c.get("sl_hit", 0) / graded
        tp = (c.get("tp1_hit", 0) + c.get("tp2_hit", 0)) / graded
        out["by_direction"][d] = {
            "n": graded,
            "sl_rate": round(sl, 3),
            "tp1_reach": round(tp, 3),
            "edge_pp": round((0.52 - sl) * 100, 1),
        }
    return out


# Early exits we want to judge against the hold-to-target backtest.
_EARLY_EXIT_REASONS = frozenset(
    {"lifecycle_stale", "bias_flip", "bounce_invalidate", "reclaim_invalidation",
     "support_lost", "trend_exhaustion", "opposite_signal"}
)


def early_exit_verdict(path: Path | None = None) -> dict[str, Any]:
    """Does the tracker's early-exit policy save us from stops or rob us of targets?

    Joins each LIVE early-exit close (lifecycle_stale etc.) with what the
    hold-to-target backtest says would have happened. Reads backtest_outcomes.jsonl
    where rows carry both ``close_reason`` (live) and ``bt_outcome`` (hold-to-target).

    Verdict per early exit:
      avoided_stop   — bt_outcome=sl_hit  → early exit was RIGHT (dodged a stop)
      forfeited_tp   — bt_outcome=tp1/tp2 → early exit was WRONG (left a winner)
      neutral        — bt_outcome=timeout → would have gone nowhere
    """
    from hunt_watch.paths import BACKTEST_OUTCOMES  # late import to avoid circular

    rows = _load_history_jsonl(path or BACKTEST_OUTCOMES)
    live_early = [
        r
        for r in rows
        if r.get("source") == "signal_history"
        and str(r.get("close_reason")) in _EARLY_EXIT_REASONS
        and r.get("bt_outcome") not in (None, "no_data")
    ]
    avoided_stop = forfeited_tp = neutral = 0
    detail: list[dict[str, Any]] = []
    for r in live_early:
        bt = str(r.get("bt_outcome"))
        if bt == "sl_hit":
            verdict = "avoided_stop"
            avoided_stop += 1
        elif bt in ("tp1_hit", "tp2_hit"):
            verdict = "forfeited_tp"
            forfeited_tp += 1
        else:
            verdict = "neutral"
            neutral += 1
        detail.append(
            {
                "symbol": r.get("symbol"),
                "direction": r.get("direction"),
                "close_reason": r.get("close_reason"),
                "bt_outcome": bt,
                "verdict": verdict,
                "live_pnl_pct": r.get("pnl_pct"),
                "bt_mfe_pct": r.get("bt_mfe_pct"),
            }
        )
    n = len(live_early)
    if n == 0:
        summary = "early-exit verdict: нет graded early-exit live сигналов (нужен backtest над signal_history)"
    else:
        net = "net-POSITIVE (cutting losers)" if avoided_stop > forfeited_tp else (
            "net-NEGATIVE (cutting winners)" if forfeited_tp > avoided_stop else "net-neutral"
        )
        summary = (
            f"early-exit verdict (n={n}): avoided {avoided_stop} stops / "
            f"forfeited {forfeited_tp} targets / {neutral} neutral → {net}"
        )
    return {
        "n": n,
        "avoided_stop": avoided_stop,
        "forfeited_tp": forfeited_tp,
        "neutral": neutral,
        "summary": summary,
        "detail": detail,
    }


def _load_history_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load signal_history.jsonl; skip malformed lines."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def compute_tp1_analysis(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze TP1 progress distribution — key calibration input for target distance.

    Returns a summary dict:
      n, median_progress, p25, p75, missed_tp1_count (reached >80% but exited early),
      suggest_closer_tp1 (bool), suggestion text.
    """
    progresses = [
        float(r["tp1_progress_pct"])
        for r in history
        if r.get("tp1_progress_pct") is not None
        and r.get("close_reason") not in ("tp1", "tp2")  # exclude actual TP hits
    ]
    if len(progresses) < 5:
        return {"n": len(progresses), "suggestion": "недостаточно данных для анализа TP1 progress"}

    progresses.sort()
    n = len(progresses)
    median = progresses[n // 2]
    p25 = progresses[n // 4]
    p75 = progresses[n * 3 // 4]
    missed = sum(1 for p in progresses if p >= 80.0)

    # If median progress > 60% before early exit → targets are reachable but we exit early
    suggest_closer = median < 30.0  # if we rarely even get halfway, TP1 might be too far
    suggestion = (
        f"TP1 progress при ранних выходах: median={median:.0f}% p25={p25:.0f}% p75={p75:.0f}% "
        f"missed≥80%={missed}/{n}"
    )
    if missed >= 3:
        suggestion += " → сигналы доходят до TP1 но выходим рано (lifecycle_stale/bias_flip)"
    if suggest_closer:
        suggestion += " → TP1 слишком далеко, рассмотреть уменьшение до 25% fib"

    return {
        "n": n,
        "median_progress": round(median, 1),
        "p25": round(p25, 1),
        "p75": round(p75, 1),
        "missed_tp1_count": missed,
        "suggest_closer_tp1": suggest_closer,
        "suggestion": suggestion,
    }


def _fuel_bucket(fuel: float) -> int:
    """Return bucket lower bound (width=16)."""
    return int(fuel // FUEL_BUCKET_WIDTH) * FUEL_BUCKET_WIDTH


def _score_bucket(score: float) -> int:
    """Return bucket lower bound (width=20)."""
    return int(score // SCORE_BUCKET_WIDTH) * SCORE_BUCKET_WIDTH


def compute_auto_calibration(
    state: dict[str, Any],
    *,
    history_path: Path | None = None,
) -> dict[str, Any]:
    """Return suggested calibration adjustments from closed_history + signal_history.jsonl.

    Returns:
        suggestions  — human-readable list of findings
        adjustments  — dict of param -> suggested value
        safe_to_apply — True only when n>=30 and thesis_success>=70%
    """
    from hunt_watch.paths import SIGNAL_HISTORY  # late import to avoid circular
    jsonl_path = history_path or SIGNAL_HISTORY
    jsonl_records = _load_history_jsonl(jsonl_path)

    # Merge: state closed_history + jsonl, deduplicate by (symbol, direction, opened_at)
    state_history: list[dict[str, Any]] = state.get("closed_history") or []
    seen: set[tuple[str, str, str]] = set()
    history: list[dict[str, Any]] = []
    for r in state_history + jsonl_records:
        key = (str(r.get("symbol") or ""), str(r.get("direction") or ""), str(r.get("opened_at") or ""))
        if key not in seen:
            seen.add(key)
            history.append(r)
    n = len(history)

    if n < MIN_N:
        # Live data is thin (duplicate-collapsed): lean on the unbiased backtest.
        bt = compute_backtest_rates()
        eev = early_exit_verdict()
        ge = compute_gate_edge()
        msgs = [f"недостаточно live данных (n={n} уникальных, нужно >={MIN_N})"]
        if bt.get("sl_hit_rate") is not None:
            msgs.append(
                f"BACKTEST truth (hold-to-target, n={bt['n_graded']}): "
                f"sl_hit={bt['sl_hit_rate']:.1%} · tp1_reach={bt.get('tp1_reach_rate')}"
            )
        for d, g in (ge.get("by_direction") or {}).items():
            msgs.append(
                f"GATE EDGE {d} (n={g['n']}): confirmed sl={g['sl_rate']:.0%} vs raw 52% "
                f"→ {g['edge_pp']:+.0f}pp, tp1_reach={g['tp1_reach']:.0%}"
            )
        msgs.append(eev["summary"])
        return {
            "suggestions": msgs,
            "adjustments": {},
            "safe_to_apply": False,
            "backtest_rates": bt,
            "early_exit_verdict": eev,
            "gate_edge": ge,
            "n_total": n,
        }

    # --- thesis success rate ---
    thesis_counts: dict[str, int] = defaultdict(int)
    for r in history:
        reason = str(r.get("close_reason") or "unknown")
        pnl = r.get("pnl_pct")
        pnl_f = float(pnl) if pnl is not None else None
        tp1_managed = bool(r.get("tp1_managed"))
        outcome = _thesis_outcome(reason, pnl_f, tp1_managed=tp1_managed)
        thesis_counts[outcome] += 1

    success_n = thesis_counts["tp_hit"] + thesis_counts["scratch_win"]
    thesis_success = success_n / n

    # UNBIASED truth source: hold-to-target backtest. The live thesis_success is
    # inflated by the early-exit policy (never logs a stop), so it cannot veto.
    bt = compute_backtest_rates()
    bt_sl_rate = bt.get("sl_hit_rate")
    bt_n = int(bt.get("n_graded") or 0)
    backtest_blocks = (
        bt_sl_rate is not None and bt_n >= MIN_BACKTEST_N and bt_sl_rate > BACKTEST_SL_GATE
    )
    safe_to_apply = (
        n >= SAFE_N and thesis_success >= THESIS_SUCCESS_FLOOR and not backtest_blocks
    )

    suggestions: list[str] = []
    adjustments: dict[str, Any] = {}

    suggestions.append(
        f"n={n} | thesis_success={thesis_success:.1%} "
        f"(tp_hit={thesis_counts['tp_hit']}, scratch_win={thesis_counts['scratch_win']}, "
        f"stop_loss={thesis_counts['stop_loss']}, thesis_fail={thesis_counts['thesis_fail']})"
    )
    if bt_sl_rate is not None:
        suggestions.append(
            f"BACKTEST truth (hold-to-target, n={bt_n}): "
            f"sl_hit={bt_sl_rate:.1%} · tp1_reach={bt.get('tp1_reach_rate')} "
            f"— live thesis_success is early-exit-biased, use this for safety"
        )

    if thesis_success < THESIS_SUCCESS_FLOOR:
        suggestions.append(
            f"GUARDRAIL: thesis_success {thesis_success:.1%} < {THESIS_SUCCESS_FLOOR:.0%} "
            "— не ослаблять пороги, safe_to_apply=False"
        )
    if backtest_blocks:
        suggestions.append(
            f"GUARDRAIL: backtest sl_hit {bt_sl_rate:.1%} > {BACKTEST_SL_GATE:.0%} "
            f"(n={bt_n}) — НЕ ослаблять пороги, safe_to_apply=False (unbiased hold-to-target)"
        )

    # --- fuel bucket analysis ---
    fuel_wins: dict[int, list[bool]] = defaultdict(list)
    for r in history:
        fuel = r.get("fuel")
        if fuel is None:
            continue
        fuel_f = float(fuel)
        reason = str(r.get("close_reason") or "unknown")
        pnl = r.get("pnl_pct")
        pnl_f = float(pnl) if pnl is not None else None
        tp1_managed = bool(r.get("tp1_managed"))
        outcome = _thesis_outcome(reason, pnl_f, tp1_managed=tp1_managed)
        is_win = outcome in ("tp_hit", "scratch_win")
        bkt = _fuel_bucket(fuel_f)
        fuel_wins[bkt].append(is_win)

    fuel_rows: list[tuple[int, float, int]] = []
    for bkt, wins in sorted(fuel_wins.items()):
        bkt_n = len(wins)
        wr = sum(wins) / bkt_n if bkt_n else 0.0
        fuel_rows.append((bkt, wr, bkt_n))
        suggestions.append(f"  fuel {bkt}-{bkt + FUEL_BUCKET_WIDTH - 1}: WR={wr:.1%} n={bkt_n}")

    # suggest forming_min if low fuel has poor WR
    low_fuel_bad = [
        (bkt, wr, bkt_n)
        for bkt, wr, bkt_n in fuel_rows
        if bkt < 60 and wr < 0.40 and bkt_n >= MIN_BUCKET_N
    ]
    if low_fuel_bad:
        suggestions.append(
            f"SUGGEST forming_min=60 (fuel<60 has WR<40% in buckets: "
            + ", ".join(f"{b}-{b+FUEL_BUCKET_WIDTH-1} WR={w:.1%} n={nn}" for b, w, nn in low_fuel_bad)
            + ")"
        )
        adjustments["forming_min"] = 60

    # --- score bucket analysis ---
    score_wins: dict[int, list[bool]] = defaultdict(list)
    for r in history:
        score = r.get("score")
        if score is None:
            continue
        score_f = float(score)
        reason = str(r.get("close_reason") or "unknown")
        pnl = r.get("pnl_pct")
        pnl_f = float(pnl) if pnl is not None else None
        tp1_managed = bool(r.get("tp1_managed"))
        outcome = _thesis_outcome(reason, pnl_f, tp1_managed=tp1_managed)
        is_win = outcome in ("tp_hit", "scratch_win")
        bkt = _score_bucket(score_f)
        score_wins[bkt].append(is_win)

    score_rows: list[tuple[int, float, int]] = []
    for bkt, wins in sorted(score_wins.items()):
        bkt_n = len(wins)
        wr = sum(wins) / bkt_n if bkt_n else 0.0
        score_rows.append((bkt, wr, bkt_n))
        suggestions.append(f"  score {bkt}-{bkt + SCORE_BUCKET_WIDTH - 1}: WR={wr:.1%} n={bkt_n}")

    # lowest score bucket vs rest
    valid_score_rows = [(bkt, wr, bkt_n) for bkt, wr, bkt_n in score_rows if bkt_n >= MIN_BUCKET_N]
    if len(valid_score_rows) >= 2:
        lowest_bkt, lowest_wr, lowest_n = valid_score_rows[0]
        rest_wins_total = sum(
            int(round(wr * bkt_n)) for _, wr, bkt_n in valid_score_rows[1:]
        )
        rest_n_total = sum(bkt_n for _, _, bkt_n in valid_score_rows[1:])
        rest_wr = rest_wins_total / rest_n_total if rest_n_total else 0.0
        delta = rest_wr - lowest_wr
        if delta > CLEAR_DELTA_WR:
            new_min = lowest_bkt + SCORE_BUCKET_WIDTH
            suggestions.append(
                f"SUGGEST confirm_min_score={new_min} "
                f"(lowest bucket score {lowest_bkt}-{lowest_bkt+SCORE_BUCKET_WIDTH-1} "
                f"WR={lowest_wr:.1%} vs rest WR={rest_wr:.1%}, delta={delta:.1%})"
            )
            adjustments["confirm_min_score"] = new_min

    # --- phase performance analysis ---
    phase_wins: dict[str, list[bool]] = defaultdict(list)
    for r in history:
        phase = str(r.get("entry_lifecycle_phase") or "unknown")
        reason = str(r.get("close_reason") or "unknown")
        pnl = r.get("pnl_pct")
        pnl_f = float(pnl) if pnl is not None else None
        tp1_managed = bool(r.get("tp1_managed"))
        outcome = _thesis_outcome(reason, pnl_f, tp1_managed=tp1_managed)
        is_win = outcome in ("tp_hit", "scratch_win")
        phase_wins[phase].append(is_win)

    best_phases: list[str] = []
    for phase, wins in sorted(phase_wins.items()):
        phase_n = len(wins)
        wr = sum(wins) / phase_n if phase_n else 0.0
        suggestions.append(f"  phase {phase}: WR={wr:.1%} n={phase_n}")
        if wr > PHASE_WIN_FLOOR and phase_n >= MIN_PHASE_N:
            best_phases.append(f"{phase} (WR={wr:.1%} n={phase_n})")

    if best_phases:
        suggestions.append(
            "SUGGEST phase_bonus для фаз: " + ", ".join(best_phases)
        )
        adjustments["phase_bonus_candidates"] = best_phases

    suggestions.append(
        f"safe_to_apply={safe_to_apply} "
        f"(n>={SAFE_N}: {n >= SAFE_N}, thesis_success>={THESIS_SUCCESS_FLOOR:.0%}: {thesis_success >= THESIS_SUCCESS_FLOOR}, "
        f"backtest_sl_ok: {not backtest_blocks})"
    )

    # --- TP1 progress analysis ---
    tp1_analysis = compute_tp1_analysis(history)
    if "suggestion" in tp1_analysis:
        suggestions.append("TP1 analysis: " + tp1_analysis["suggestion"])
        if tp1_analysis.get("suggest_closer_tp1"):
            adjustments["tp1_fib_level"] = "ret_236"  # suggest moving from 38.2% to 23.6%

    # --- early-exit policy verdict (R2) ---
    eev = early_exit_verdict()
    suggestions.append(eev["summary"])

    # --- gate edge: confirmed setups vs raw baseline ---
    gate_edge = compute_gate_edge()
    for d, g in (gate_edge.get("by_direction") or {}).items():
        suggestions.append(
            f"GATE EDGE {d} (n={g['n']}): confirmed sl={g['sl_rate']:.0%} vs raw 52% "
            f"→ {g['edge_pp']:+.0f}pp"
        )

    return {
        "suggestions": suggestions,
        "adjustments": adjustments,
        "safe_to_apply": safe_to_apply,
        "tp1_analysis": tp1_analysis,
        "backtest_rates": bt,
        "early_exit_verdict": eev,
        "gate_edge": gate_edge,
        "n_total": n,
        "n_jsonl": len(jsonl_records),
        "n_state": len(state_history),
    }

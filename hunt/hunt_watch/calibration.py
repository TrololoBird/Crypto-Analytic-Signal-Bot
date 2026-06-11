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
        return {
            "suggestions": [f"недостаточно данных (n={n}, нужно >={MIN_N})"],
            "adjustments": {},
            "safe_to_apply": False,
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
    safe_to_apply = n >= SAFE_N and thesis_success >= THESIS_SUCCESS_FLOOR

    suggestions: list[str] = []
    adjustments: dict[str, Any] = {}

    suggestions.append(
        f"n={n} | thesis_success={thesis_success:.1%} "
        f"(tp_hit={thesis_counts['tp_hit']}, scratch_win={thesis_counts['scratch_win']}, "
        f"stop_loss={thesis_counts['stop_loss']}, thesis_fail={thesis_counts['thesis_fail']})"
    )

    if thesis_success < THESIS_SUCCESS_FLOOR:
        suggestions.append(
            f"GUARDRAIL: thesis_success {thesis_success:.1%} < {THESIS_SUCCESS_FLOOR:.0%} "
            "— не ослаблять пороги, safe_to_apply=False"
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
        f"(n>={SAFE_N}: {n >= SAFE_N}, thesis_success>={THESIS_SUCCESS_FLOOR:.0%}: {thesis_success >= THESIS_SUCCESS_FLOOR})"
    )

    # --- TP1 progress analysis ---
    tp1_analysis = compute_tp1_analysis(history)
    if "suggestion" in tp1_analysis:
        suggestions.append("TP1 analysis: " + tp1_analysis["suggestion"])
        if tp1_analysis.get("suggest_closer_tp1"):
            adjustments["tp1_fib_level"] = "ret_236"  # suggest moving from 38.2% to 23.6%

    return {
        "suggestions": suggestions,
        "adjustments": adjustments,
        "safe_to_apply": safe_to_apply,
        "tp1_analysis": tp1_analysis,
        "n_total": n,
        "n_jsonl": len(jsonl_records),
        "n_state": len(state_history),
    }

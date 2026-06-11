"""Orchestrate /autotune: reconcile → tg_backtest → calibrate with tiered guardrails."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hunt_watch.param_calibration import backfill_legacy_outcomes, run_full_calibration
from hunt_watch.param_store import load_calibration, save_calibration_payload, stats_thresholds
from hunt_watch.paths import DATA, SESSION_DIR, SIGNAL_STATE
from hunt_watch.stats_report import confidence_tier
from hunt_watch.signal_tracker import load_tracker_state, save_tracker_state

LAST_AUTOTUNE_PATH = SESSION_DIR / "last_autotune.json"
AUTOTUNE_REPORT_PATH = SESSION_DIR / "autotune_report.json"
RATE_LIMIT_HOURS = 6
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYTHON = _REPO_ROOT / ".venv" / "bin" / "python"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _rate_limited() -> datetime | None:
    if not LAST_AUTOTUNE_PATH.is_file():
        return None
    try:
        raw = json.loads(LAST_AUTOTUNE_PATH.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(str(raw.get("ts")).replace("Z", "+00:00"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if datetime.now(UTC) - last < timedelta(hours=RATE_LIMIT_HOURS):
        return last
    return None


def _run_script(rel: str, *args: str) -> None:
    py = _PYTHON if _PYTHON.is_file() else Path(sys.executable)
    subprocess.run(
        [str(py), str(_REPO_ROOT / rel), *args],
        cwd=str(_REPO_ROOT),
        check=False,
        timeout=300,
    )


def _reconcile_tracker() -> int:
    state = load_tracker_state()
    filled = backfill_legacy_outcomes(state)
    if filled:
        save_tracker_state(state)
    _run_script("hunt/scripts/reconcile_signals.py")
    _run_script("hunt/scripts/reconcile_signals.py", "--backfill-legacy")
    return filled


def _apply_guardrails(
    payload: dict[str, Any],
    *,
    before_gates: dict[str, float],
    n_labeled: int,
) -> tuple[str, dict[str, Any]]:
    gates = payload.setdefault("universal", {}).setdefault("gates", {})
    diff: dict[str, Any] = {"before": dict(before_gates), "after": {}, "blocked": []}

    if n_labeled < 30:
        mode = "report_only"
        for key in ("confirm_min_score", "confirm_min_score_no_div", "forming_min_score"):
            if key in before_gates:
                gates[key] = before_gates[key]
                diff["blocked"].append(key)
    elif n_labeled < 100:
        mode = "conservative"
        for key in ("confirm_min_score", "confirm_min_score_no_div", "forming_min_score"):
            if key in before_gates:
                gates[key] = before_gates[key]
                diff["blocked"].append(key)
        if "adx_trend_block" in gates and "adx_trend_block" in before_gates:
            gates["adx_trend_block"] = round(
                _clamp(
                    float(gates["adx_trend_block"]),
                    float(before_gates["adx_trend_block"]) - 2.0,
                    float(before_gates["adx_trend_block"]) + 2.0,
                ),
                1,
            )
    elif n_labeled < 200:
        mode = "calibrated"
        for key in ("confirm_min_score", "confirm_min_score_no_div"):
            if key in before_gates:
                gates[key] = round(
                    _clamp(
                        float(gates[key]),
                        float(before_gates[key]) - 3.0,
                        float(before_gates[key]) + 3.0,
                    ),
                    1,
                )
    else:
        mode = "production"

    diff["after"] = {k: gates.get(k) for k in gates}
    diff["mode"] = mode
    payload["autotune"] = {
        "mode": mode,
        "tier": confidence_tier(n_labeled),
        "n_labeled": n_labeled,
        "diff": diff,
    }
    return mode, diff


def run_autotune(*, force: bool = False) -> dict[str, Any]:
    """Sync autotune pipeline. Returns report dict for Telegram/HTML."""
    last = _rate_limited()
    if last and not force:
        return {
            "ok": False,
            "reason": "rate_limit",
            "last_run": last.isoformat(),
            "retry_after_h": RATE_LIMIT_HOURS,
        }

    before = load_calibration()
    before_gates = dict((before.get("universal") or {}).get("gates") or {})

    legacy_filled = _reconcile_tracker()
    fwd_h = stats_thresholds().get("forward_horizon_hours", 8.0)
    _run_script("hunt/scripts/tg_backtest.py", "--hours", str(fwd_h))

    payload = run_full_calibration(fetch_rest=True, backfill=True, rest_symbol_limit=30)
    n_labeled = int((payload.get("data_summary") or {}).get("n_labeled") or 0)
    mode, diff = _apply_guardrails(payload, before_gates=before_gates, n_labeled=n_labeled)
    save_calibration_payload(payload)

    report = {
        "ok": True,
        "ts": datetime.now(UTC).isoformat(),
        "mode": mode,
        "tier": confidence_tier(n_labeled),
        "n_labeled": n_labeled,
        "legacy_backfill": legacy_filled,
        "diff": diff,
        "data_summary": payload.get("data_summary"),
        "outcome_calibration": payload.get("outcome_calibration"),
    }
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    AUTOTUNE_REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    LAST_AUTOTUNE_PATH.write_text(
        json.dumps({"ts": report["ts"], "mode": mode, "n_labeled": n_labeled}, indent=2),
        encoding="utf-8",
    )
    return report


def format_autotune_html(report: dict[str, Any]) -> str:
    if not report.get("ok"):
        if report.get("reason") == "rate_limit":
            return (
                f"⏳ <b>/autotune</b> — rate limit {report.get('retry_after_h')}h\n"
                f"Последний: <code>{report.get('last_run', '—')}</code>\n"
                f"<i>Используй force только из CLI</i>"
            )
        return f"⚠️ /autotune failed: <code>{report.get('reason', 'unknown')}</code>"

    diff = report.get("diff") or {}
    before = diff.get("before") or {}
    after = diff.get("after") or {}
    lines = [
        f"✅ <b>/autotune</b> · {report.get('mode')} · {report.get('tier')}",
        f"n_labeled <code>{report.get('n_labeled')}</code> · "
        f"legacy backfill <code>{report.get('legacy_backfill', 0)}</code>",
        "<b>Gates diff:</b>",
    ]
    for key in ("confirm_min_score", "adx_trend_block", "forming_min_score"):
        b, a = before.get(key), after.get(key)
        if b is None and a is None:
            continue
        mark = " 🔒" if key in (diff.get("blocked") or []) else ""
        lines.append(f"· <code>{key}</code> {b} → {a}{mark}")
    oc = report.get("outcome_calibration") or {}
    if oc:
        lines.append(
            f"<b>Outcomes:</b> wins <code>{oc.get('n_wins')}</code> · "
            f"stops <code>{oc.get('n_stops')}</code>"
        )
    lines.append(f"<i>Report:</i> <code>hunt/data/session/autotune_report.json</code>")
    lines.append("<i>Hot-reload on next watch tick · не auto-trade</i>")
    return "\n".join(lines)

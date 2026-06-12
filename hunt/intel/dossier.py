"""Build offline intel dossier from stats + latched features (Layer 2 → Layer 3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_watch.calibration import _load_history_jsonl, _thesis_outcome, compute_auto_calibration
from hunt_watch.param_store import UNIVERSAL_DEFAULTS, load_calibration
from hunt_watch.paths import (
    BACKTEST_OUTCOMES,
    HUNT_CALIBRATION,
    INTEL_DOSSIER_JSON,
    INTEL_DOSSIER_MD,
    SIGNAL_HISTORY,
    SIGNAL_STATE,
)
from hunt_watch.signal_tracker import load_tracker_state


def _load_backtest_summary(path: Path = BACKTEST_OUTCOMES) -> dict[str, Any]:
    if not path.exists():
        return {"n": 0, "outcomes": {}}
    outcomes: dict[str, int] = {}
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        n += 1
        oc = str(row.get("bt_outcome") or "unknown")
        outcomes[oc] = outcomes.get(oc, 0) + 1
    return {"n": n, "outcomes": outcomes, "path": str(path)}


def _feature_win_loss_table(history: list[dict[str, Any]], *, max_rows: int = 12) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in history:
        fo = r.get("features_open")
        if not isinstance(fo, dict):
            continue
        market = fo.get("market") if isinstance(fo.get("market"), dict) else {}
        reason = str(r.get("close_reason") or "unknown")
        pnl = r.get("pnl_pct")
        pnl_f = float(pnl) if pnl is not None else None
        outcome = _thesis_outcome(reason, pnl_f, tp1_managed=bool(r.get("tp1_managed")))
        win = outcome in ("tp_hit", "scratch_win")
        rows.append(
            {
                "symbol": r.get("symbol"),
                "direction": r.get("direction"),
                "win": win,
                "outcome": outcome,
                "close_reason": reason,
                "pnl_pct": pnl_f,
                "depth_imbalance": market.get("depth_imbalance"),
                "oi_z": market.get("oi_z"),
                "funding_zscore_48h": market.get("funding_zscore_48h"),
                "lifecycle_phase": fo.get("lifecycle_phase"),
                "fall_from_high_pct": fo.get("fall_from_high_pct"),
            }
        )
    return rows[-max_rows:]


def build_intel_dossier(
    *,
    state_path: Path = SIGNAL_STATE,
    history_path: Path = SIGNAL_HISTORY,
) -> dict[str, Any]:
    """Gather Layer-2 stats + feature table + thresholds → dossier dict."""
    state = load_tracker_state(state_path)
    history = _load_history_jsonl(history_path)
    calib = compute_auto_calibration(state, history_path=history_path)
    backtest = _load_backtest_summary()
    thresholds = load_calibration(HUNT_CALIBRATION) if HUNT_CALIBRATION.exists() else {}
    universal = thresholds.get("universal") or UNIVERSAL_DEFAULTS

    feature_table = _feature_win_loss_table(history)
    wins = sum(1 for r in feature_table if r.get("win"))
    losses = len(feature_table) - wins

    dossier: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "n_live_closed": calib.get("n_total", len(history)),
        "n_jsonl": calib.get("n_jsonl", len(history)),
        "calibration": calib,
        "backtest_summary": backtest,
        "thresholds_universal": universal,
        "feature_win_loss": {
            "n_with_features_open": len(feature_table),
            "wins": wins,
            "losses": losses,
            "rows": feature_table,
        },
        "analyst_instructions": {
            "role": "offline research analyst — propose only, never apply",
            "output_schema": {
                "hypotheses": "ranked patterns winners vs losers",
                "threshold_suggestions": "param, value, rationale, confidence",
                "strategy_gaps": "missed setups / zero-hit triage",
                "risk_flags": "overfitting / small-n cautions",
            },
            "guardrails": [
                "Do not write hunt_calibration.json or edit code",
                "Numeric suggestions must respect safe_to_apply from calibration",
                f"Refuse strong claims when n<{calib.get('n_total', 0)}",
            ],
        },
    }
    return dossier


def _render_markdown(dossier: dict[str, Any]) -> str:
    cal = dossier.get("calibration") or {}
    bt = dossier.get("backtest_summary") or {}
    fw = dossier.get("feature_win_loss") or {}
    lines = [
        "# Hunt Intel Dossier",
        "",
        f"Generated: {dossier.get('generated_at')}",
        f"Live closed signals: **{dossier.get('n_live_closed')}**",
        f"Backtest rows: **{bt.get('n', 0)}**",
        "",
        "## Calibration (deterministic Layer 2)",
        "",
        f"- safe_to_apply: `{cal.get('safe_to_apply')}`",
        "",
    ]
    for s in cal.get("suggestions") or []:
        lines.append(f"- {s}")
    lines.extend(["", "## Backtest outcome mix", ""])
    for k, v in sorted((bt.get("outcomes") or {}).items(), key=lambda kv: -kv[1]):
        lines.append(f"- {k}: {v}")
    bt_rates = cal.get("backtest_rates") or {}
    if bt_rates.get("sl_hit_rate") is not None:
        lines.extend(
            [
                "",
                "> **Truth-signal note:** the live close stats never record a stop (the tracker's",
                "> early-exit policy closes before SL), so live `thesis_success` is biased high.",
                f"> The unbiased hold-to-target backtest (n={bt_rates.get('n_graded')}) shows",
                f"> **sl_hit={bt_rates.get('sl_hit_rate')}**, tp1_reach={bt_rates.get('tp1_reach_rate')}.",
                "> Anchor any loosening decision on the backtest sl_hit rate, not the live metric.",
            ]
        )
    ge = cal.get("gate_edge") or {}
    if ge.get("by_direction"):
        lines.extend(
            [
                "",
                "## Gate edge — does the confirm gate beat the raw universe?",
                "",
                f"Raw-fade baseline SL ≈ {ge.get('raw_baseline_sl')}. Confirmed setups (kline-graded):",
                "",
                "| direction | n | confirmed SL | TP1-reach | edge vs raw |",
                "|-----------|---|--------------|-----------|-------------|",
            ]
        )
        for d, g in ge["by_direction"].items():
            lines.append(
                f"| {d} | {g['n']} | {g['sl_rate']:.0%} | {g['tp1_reach']:.0%} | {g['edge_pp']:+.0f}pp |"
            )
        lines.append("")
        lines.append("> The gate IS the edge. Keep it strict; do not loosen confirm thresholds.")

    eev = cal.get("early_exit_verdict") or {}
    if eev.get("summary"):
        lines.extend(["", "## Early-exit policy verdict (R2)", "", f"- {eev['summary']}"])
        for d in (eev.get("detail") or [])[:10]:
            lines.append(
                f"  - {d.get('symbol')} {d.get('direction')}: live `{d.get('close_reason')}` "
                f"vs hold→`{d.get('bt_outcome')}` ⇒ **{d.get('verdict')}**"
            )
    lines.extend(
        [
            "",
            "## Feature table (latched at entry)",
            "",
            f"Rows with features_open: {fw.get('n_with_features_open')} (W={fw.get('wins')} L={fw.get('losses')})",
            "",
            "| symbol | dir | win | depth_imb | oi_z | phase | pnl% |",
            "|--------|-----|-----|-----------|------|-------|------|",
        ]
    )
    for r in fw.get("rows") or []:
        lines.append(
            f"| {r.get('symbol')} | {r.get('direction')} | {r.get('win')} | "
            f"{r.get('depth_imbalance')} | {r.get('oi_z')} | {r.get('lifecycle_phase')} | "
            f"{r.get('pnl_pct')} |"
        )
    lines.extend(
        [
            "",
            "## Analyst task",
            "",
            "Return JSON matching schema: hypotheses, threshold_suggestions, strategy_gaps, risk_flags, meta.",
            "Save as `hunt/data/intel_report.json` — suggestions only.",
            "",
            "## Current universal thresholds (excerpt)",
            "",
            "```json",
            json.dumps((dossier.get("thresholds_universal") or {}).get("gates", {}), indent=2),
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def write_intel_dossier(
    dossier: dict[str, Any] | None = None,
    *,
    md_path: Path = INTEL_DOSSIER_MD,
    json_path: Path = INTEL_DOSSIER_JSON,
) -> tuple[Path, Path]:
    dossier = dossier or build_intel_dossier()
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_markdown(dossier), encoding="utf-8")
    json_path.write_text(json.dumps(dossier, indent=2, default=str), encoding="utf-8")
    return md_path, json_path

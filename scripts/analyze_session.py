"""Simple session analyzer for telemetry runs.

Usage:
  python scripts/analyze_session.py --run-dir <path-to-run-dir>

The script reads `raw/signals.jsonl` and `analysis/outcomes.jsonl` (if present),
aggregates simple metrics and writes `report.json` and `report.md` into the
run's `analysis/` folder (or the provided `--out-dir`).
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from engine.errors import DEFENSIVE_EXC


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path or not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except DEFENSIVE_EXC:
                continue
    return rows


def _find_file(run_dir: Path, names: list[str]) -> Path | None:
    candidates = [run_dir / "raw", run_dir / "analysis", run_dir]
    for base in candidates:
        for name in names:
            p = base / name
            if p.exists():
                return p
    return None


def _aggregate(signals: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes_by_signal: dict[str, list[dict[str, Any]]] = {}
    for o in outcomes:
        key = o.get("signal_id") or o.get("tracking_id") or o.get("tracking_ref")
        if key:
            outcomes_by_signal.setdefault(key, []).append(o)

    per_setup: dict[str, dict[str, Any]] = {}
    for s in signals:
        setup = s.get("setup_id") or s.get("strategy") or "<unknown>"
        rec = per_setup.setdefault(
            setup, {"signals": 0, "outcomes": 0, "stop_loss": 0, "tp": 0, "pnls": []}
        )
        rec["signals"] += 1
        key = s.get("tracking_ref") or s.get("signal_id")
        outs = outcomes_by_signal.get(key, [])
        for o in outs:
            rec["outcomes"] += 1
            res = (o.get("result") or "").lower()
            if res.startswith("stop"):
                rec["stop_loss"] += 1
            else:
                rec["tp"] += 1
            try:
                if o.get("pnl_r_multiple") is not None:
                    rec["pnls"].append(float(o.get("pnl_r_multiple") or 0.0))
            except DEFENSIVE_EXC:
                pass

    for v in per_setup.values():
        v["sl_rate"] = (v["stop_loss"] / v["outcomes"] * 100.0) if v["outcomes"] else None
        v["tp_rate"] = (v["tp"] / v["outcomes"] * 100.0) if v["outcomes"] else None
        v["avg_r"] = statistics.mean(v["pnls"]) if v["pnls"] else None

    result_counts: dict[str, int] = {}
    for o in outcomes:
        r = o.get("result") or "unknown"
        result_counts[r] = result_counts.get(r, 0) + 1

    return {
        "total_signals": len(signals),
        "total_outcomes": len(outcomes),
        "result_counts": result_counts,
        "per_setup": per_setup,
    }


def _write_report(report: dict[str, Any], out_json: Path, out_md: Path) -> None:
    with out_json.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)

    with out_md.open("w", encoding="utf-8") as fh:
        fh.write("# Session Analysis\n\n")
        fh.write(f"- Total signals: {report.get('total_signals')}\n")
        fh.write(f"- Total outcomes: {report.get('total_outcomes')}\n\n")
        fh.write("## Result counts\n")
        for k, v in report.get("result_counts", {}).items():
            fh.write(f"- {k}: {v}\n")
        fh.write("\n## Per-setup summary\n")
        for setup, vals in report.get("per_setup", {}).items():
            fh.write(f"### {setup}\n")
            fh.write(f"- Signals: {vals.get('signals')}\n")
            fh.write(f"- Outcomes: {vals.get('outcomes')}\n")
            if vals.get("sl_rate") is not None:
                fh.write(f"- SL rate: {vals.get('sl_rate'):.1f}%\n")
            if vals.get("avg_r") is not None:
                fh.write(f"- Avg R: {vals.get('avg_r'):.2f}\n")
            fh.write("\n")


def analyze_run(run_dir: str | Path, out_dir: str | Path | None = None) -> tuple[str, str]:
    run_dir = Path(run_dir)
    if not run_dir.exists():
        msg = f"run_dir not found: {run_dir}"
        raise FileNotFoundError(msg)

    signals_path = _find_file(run_dir, ["signals.jsonl"]) or run_dir / "raw" / "signals.jsonl"
    outcomes_path = (
        _find_file(run_dir, ["outcomes.jsonl"]) or run_dir / "analysis" / "outcomes.jsonl"
    )

    signals = _read_jsonl(signals_path) if signals_path and signals_path.exists() else []
    outcomes = _read_jsonl(outcomes_path) if outcomes_path and outcomes_path.exists() else []

    report = _aggregate(signals, outcomes)

    out_dir = Path(out_dir) if out_dir else (run_dir / "analysis")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / "report.json"
    out_md = out_dir / "report.md"
    _write_report(report, out_json, out_md)
    return str(out_json), str(out_md)


def _cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--out-dir", required=False)
    args = parser.parse_args()
    analyze_run(args.run_dir, args.out_dir)


if __name__ == "__main__":
    _cli()

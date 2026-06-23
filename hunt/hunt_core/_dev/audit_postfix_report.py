#!/usr/bin/env python3
"""Post-fix soak report — hunt_scan JSONL + audit planes vs pre-fix baseline."""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from hunt_core._dev.check_data_plane import (
    summarize_data_plane,
    summarize_universe,
    _read_jsonl,
)
from hunt_core.paths import (
    DATA,
    DATA_PLANE_AUDIT_JSONL,
    HUNT_SCAN_JSONL,
    RECONCILE_PATH_SHADOW_JSONL,
    RR_GEOMETRY_AUDIT_JSONL,
    UNIVERSE_AUDIT_JSONL,
)


def _latest_scan_jsonl() -> Path | None:
    candidates = sorted(DATA.glob("hunt_scan-*.jsonl"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def summarize_hunt_scan(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    phases = Counter()
    leg_gains: list[float] = []
    playbook_pass = 0
    playbook_seen = 0
    patterns = Counter()
    deep_wait = 0
    deep_seen = 0

    for row in rows:
        lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
        phase = str(lc.get("phase_fusion") or lc.get("phase") or "none")
        phases[phase] += 1
        lg = lc.get("leg_gain_pct")
        if lg is not None:
            try:
                leg_gains.append(float(lg))
            except (TypeError, ValueError):
                pass
        mf = row.get("manipulation_fusion")
        if isinstance(mf, dict):
            playbook_seen += 1
            pc = int(mf.get("pass_count") or 0)
            req = int(mf.get("required_n") or 0)
            if mf.get("playbook_passes") or (req > 0 and pc >= req):
                playbook_pass += 1
        v2 = row.get("verdict_v2")
        if isinstance(v2, dict):
            deep_seen += 1
            if str(v2.get("action") or "").lower() == "wait":
                deep_wait += 1
            pat = v2.get("pattern")
            if pat:
                patterns[str(pat)] += 1

    return {
        "n": len(rows),
        "phase_mix": dict(phases),
        "leg_gain_median_pct": round(statistics.median(leg_gains), 2) if leg_gains else None,
        "pct_leg_gain_gt_5": round(100.0 * sum(1 for x in leg_gains if x > 5.0) / len(leg_gains), 1)
        if leg_gains
        else None,
        "playbook_pass_pct": round(100.0 * playbook_pass / playbook_seen, 1) if playbook_seen else None,
        "deep_wait_pct": round(100.0 * deep_wait / deep_seen, 1) if deep_seen else None,
        "verdict_patterns_top": patterns.most_common(6),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Hunt post-fix audit report")
    parser.add_argument("--scan", type=Path, default=None, help="hunt_scan JSONL (default: latest daily)")
    parser.add_argument("--baseline", type=Path, default=None, help="pre-fix hunt_scan for A/B")
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()

    scan_path = args.scan or _latest_scan_jsonl() or HUNT_SCAN_JSONL
    scan_rows = _read_jsonl(scan_path, limit=args.limit)
    baseline_rows = _read_jsonl(args.baseline, limit=args.limit) if args.baseline else []

    report: dict[str, Any] = {
        "scan_path": str(scan_path),
        "hunt_scan": summarize_hunt_scan(scan_rows),
        "data_plane": summarize_data_plane(_read_jsonl(DATA_PLANE_AUDIT_JSONL, limit=args.limit)),
        "universe": summarize_universe(_read_jsonl(UNIVERSE_AUDIT_JSONL, limit=args.limit)),
    }
    rr_rows = _read_jsonl(RR_GEOMETRY_AUDIT_JSONL, limit=args.limit)
    if rr_rows:
        rr_vals = [float(r["rr_primary"]) for r in rr_rows if r.get("rr_primary") is not None]
        report["rr_geometry"] = {
            "n": len(rr_rows),
            "median_rr_primary": round(statistics.median(rr_vals), 2) if rr_vals else None,
        }
    shadow = _read_jsonl(RECONCILE_PATH_SHADOW_JSONL, limit=args.limit)
    if shadow:
        report["reconcile_shadow_n"] = len(shadow)

    if baseline_rows:
        report["baseline"] = {
            "path": str(args.baseline),
            "hunt_scan": summarize_hunt_scan(baseline_rows),
        }

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

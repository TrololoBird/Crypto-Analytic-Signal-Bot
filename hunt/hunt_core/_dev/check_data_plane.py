#!/usr/bin/env python3
"""P0 probe helper — summarize data_plane_audit + universe_audit JSONL."""
from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from hunt_core.paths import (
    DATA_PLANE_AUDIT_JSONL,
    RECONCILE_PATH_SHADOW_JSONL,
    RR_GEOMETRY_AUDIT_JSONL,
    UNIVERSE_AUDIT_JSONL,
)


def _read_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if limit is not None and len(rows) >= limit:
                break
    return rows


def summarize_data_plane(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    ws_eq_rest_oi: list[bool] = []
    rest_ages: list[float] = []
    ws_ages: list[float] = []
    stale_counts: list[int] = []
    field_stale: Counter[str] = Counter()

    for row in rows:
        summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
        if summary.get("median_rest_age_s") is not None:
            rest_ages.append(float(summary["median_rest_age_s"]))
        if summary.get("median_ws_age_s") is not None:
            ws_ages.append(float(summary["median_ws_age_s"]))
        stale_counts.append(int(summary.get("stale_field_count") or 0))
        for field in row.get("fields") or []:
            if not isinstance(field, dict):
                continue
            if field.get("stale"):
                field_stale[str(field.get("field"))] += 1
            if field.get("field") == "oi" and field.get("age_s") is not None:
                ws_age = row.get("ws_last_msg_age_s")
                if ws_age is not None:
                    ws_eq_rest_oi.append(abs(float(field["age_s"]) - float(ws_age)) < 0.05)

    out: dict[str, Any] = {
        "n": len(rows),
        "median_rest_age_s": round(statistics.median(rest_ages), 2) if rest_ages else None,
        "median_ws_age_s": round(statistics.median(ws_ages), 2) if ws_ages else None,
        "median_stale_fields": round(statistics.median(stale_counts), 1) if stale_counts else None,
        "oi_age_equals_ws_pct": round(100.0 * sum(ws_eq_rest_oi) / len(ws_eq_rest_oi), 1)
        if ws_eq_rest_oi
        else None,
        "top_stale_fields": field_stale.most_common(8),
    }
    return out


def summarize_universe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    ticks = [r for r in rows if r.get("event") == "tick_snapshot"]
    prescans = [r for r in rows if r.get("event") == "prescan_ready"]
    merge_skips = [r for r in rows if r.get("event") == "prescan_merge_skip"]
    phases = Counter(str(r.get("phase") or "none") for r in ticks)
    leg_gains = [
        float(r["leg_gain_pct"])
        for r in ticks
        if r.get("leg_gain_pct") is not None
    ]
    mid_leg = [
        float(r["leg_gain_pct"])
        for r in ticks
        if str(r.get("phase")) == "mid" and r.get("leg_gain_pct") is not None
    ]
    pre_pump_leg = [
        float(r["leg_gain_pct"])
        for r in ticks
        if str(r.get("phase")) == "pre_pump" and r.get("leg_gain_pct") is not None
    ]
    cusum_vals = [
        abs(float(r["cusum"]))
        for r in ticks
        if r.get("cusum") is not None
    ]
    cusum_clip_hits = sum(1 for v in cusum_vals if v >= 495.0)
    late_skip_chg = [
        abs(float(r["change_pct"]))
        for r in merge_skips
        if r.get("change_pct") is not None
    ]
    return {
        "n": len(rows),
        "tick_snapshots": len(ticks),
        "prescan_ready": len(prescans),
        "prescan_merge_skip": len(merge_skips),
        "prescan_merge_skip_late_chase": sum(
            1 for r in merge_skips if r.get("reason") == "late_chase"
        ),
        "merge_skip_change_pct_median": round(statistics.median(late_skip_chg), 2)
        if late_skip_chg
        else None,
        "phase_mix": dict(phases),
        "leg_gain_median_pct": round(statistics.median(leg_gains), 2) if leg_gains else None,
        "mid_leg_gain_median_pct": round(statistics.median(mid_leg), 2) if mid_leg else None,
        "pre_pump_leg_gain_median_pct": round(statistics.median(pre_pump_leg), 2)
        if pre_pump_leg
        else None,
        "pct_leg_gain_gt_5": round(
            100.0 * sum(1 for x in leg_gains if x > 5.0) / len(leg_gains), 1
        )
        if leg_gains
        else None,
        "cusum_clip_saturation_pct": round(100.0 * cusum_clip_hits / len(cusum_vals), 1)
        if cusum_vals
        else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize Hunt P0 audit JSONL")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--data-plane", type=Path, default=DATA_PLANE_AUDIT_JSONL)
    parser.add_argument("--universe", type=Path, default=UNIVERSE_AUDIT_JSONL)
    parser.add_argument("--rr-geometry", type=Path, default=RR_GEOMETRY_AUDIT_JSONL)
    parser.add_argument("--reconcile-shadow", type=Path, default=RECONCILE_PATH_SHADOW_JSONL)
    args = parser.parse_args()

    dp_rows = _read_jsonl(args.data_plane, limit=args.limit)
    uni_rows = _read_jsonl(args.universe, limit=args.limit)
    rr_rows = _read_jsonl(args.rr_geometry, limit=args.limit)
    shadow_rows = _read_jsonl(args.reconcile_shadow, limit=args.limit)

    print("=== data_plane_audit ===")
    print(json.dumps(summarize_data_plane(dp_rows), indent=2))
    print("\n=== universe_audit ===")
    print(json.dumps(summarize_universe(uni_rows), indent=2))
    if rr_rows:
        rr_primary = [float(r["rr_primary"]) for r in rr_rows if r.get("rr_primary") is not None]
        print("\n=== rr_geometry_audit ===")
        print(
            json.dumps(
                {
                    "n": len(rr_rows),
                    "median_rr_primary": round(sorted(rr_primary)[len(rr_primary) // 2], 2)
                    if rr_primary
                    else None,
                    "wait_pct": round(
                        100.0
                        * sum(1 for r in rr_rows if str(r.get("action")) == "wait")
                        / len(rr_rows),
                        1,
                    ),
                },
                indent=2,
            )
        )
    if shadow_rows:
        print(f"\n=== reconcile_path_shadow (n={len(shadow_rows)}) ===")
    print(
        f"\npaths: data_plane={args.data_plane} universe={args.universe} "
        f"rr={args.rr_geometry} shadow={args.reconcile_shadow}"
    )


if __name__ == "__main__":
    main()

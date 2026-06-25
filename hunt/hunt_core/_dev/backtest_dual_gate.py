#!/usr/bin/env python3
"""Backtest Dual-Gate counterfactual on existing outcome_ledger + hunt_scan data.

Usage:
    python -m hunt_core._dev.backtest_dual_gate

Reads:
    - hunt_outcome_ledger.jsonl (484 rows, broad population)
    - hunt_scan.jsonl (rows with full microstructure for energy+structure calcs)

Outputs:
    - How many pre-phase candidates pass the NEW pre_gate (vs old gate)
    - Distribution by phase, blocker, fusion_score
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# ── Dual-Gate thresholds (from fusion.py) ──────────────────────────
PRE_GATE_MIN_ENERGY = 3
PRE_GATE_MIN_STRUCTURE = 0.18
PRE_GATE_MIN_MAGNITUDE = 0.15
FUSION_SCORE_SCALE = 25.0  # fusion_score = magnitude * scale


# ── Helpers ────────────────────────────────────────────────────────
def _data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def _f(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default  # noqa: PLR0124
    except (TypeError, ValueError):
        return default


def magnitude_from_fusion_score(fs: float) -> float:
    """Reverse of fusion_score = magnitude * 25."""
    return fs / FUSION_SCORE_SCALE


def estimate_energy_from_ledger_row(row: dict[str, Any]) -> int:
    """Rough energy proxy from outcome_ledger fields (no microstructure)."""
    hits = 0
    # playbook_pass_ratio: fraction of 5 checks met
    # If >= 3/5 checks pass → high energy
    pbr = _f(row.get("playbook_pass_ratio"))
    if pbr >= 0.6:  # 3 of 5
        hits += 3
    elif pbr >= 0.4:  # 2 of 5
        hits += 2
    elif pbr >= 0.2:  # 1 of 5
        hits += 1
    # check_sources — count how many of the 20 domains are present
    cs = row.get("check_sources") if isinstance(row.get("check_sources"), dict) else {}
    if len(cs) >= 15:
        hits += 1
    return min(hits, 4)


def estimate_structure_from_ledger(row: dict[str, Any]) -> float:
    """Rough structure proxy — fusion_score indicates directional conviction."""
    fs = _f(row.get("fusion_score"))
    # Low fusion_score < 5 → weak structure; high > 15 → strong
    return min(1.0, max(0.0, fs / 50.0))  # scale 0-1, 50 = max


def energy_from_hunt_scan(market: dict[str, Any]) -> int:
    """Full energy calculation from hunt_scan market data (same as _mission.py)."""
    hits = 0
    oi_z = _f(market.get("oi_z"))
    if oi_z >= 0.8:
        hits += 1
    acc = _f(market.get("map_accumulation_score"))
    if acc >= 0.45:
        hits += 1
    imb = abs(_f(market.get("depth_imbalance")))
    if imb >= 0.12:
        hits += 1
    ac = int(market.get("map_absorption_count") or 0)
    sw = int(market.get("map_sticky_wall_count") or 0)
    if ac >= 1 or sw >= 1:
        hits += 1
    return hits


def structure_from_hunt_scan(market: dict[str, Any]) -> float:
    """Structure = abs(depth_imbalance) — independent of factor fusion."""
    return abs(_f(market.get("depth_imbalance")))


def pre_gate_decision(
    energy_hits: int,
    structure_score: float,
    magnitude: float,
) -> tuple[bool, str]:
    """Same logic as pre_phase_gate() in fusion.py."""
    if energy_hits < PRE_GATE_MIN_ENERGY:
        return False, f"low_energy:{energy_hits}/{PRE_GATE_MIN_ENERGY}"
    if structure_score < PRE_GATE_MIN_STRUCTURE:
        return False, f"low_structure:{structure_score:.2f}/{PRE_GATE_MIN_STRUCTURE}"
    if magnitude < PRE_GATE_MIN_MAGNITUDE:
        return False, f"low_magnitude:{magnitude:.2f}/{PRE_GATE_MIN_MAGNITUDE}"
    return True, "pre_gate_open"


def old_gate_decision(fusion_score: float) -> tuple[bool, str]:
    """Old single gate: magnitude >= 0.5 (abs_floor) + calibrated quantile.

    The calibrated gate additionally requires the symbol's quantile threshold.
    For backtest purposes we check the abs_floor (0.5) — a necessary condition.
    """
    mag = magnitude_from_fusion_score(fusion_score)
    if mag < 0.5:
        return False, f"below_abs_floor:{mag:.2f}/0.5"
    return True, "above_abs_floor"


# ── Analysis ──────────────────────────────────────────────────────

def analyze_outcome_ledger(path: Path) -> None:
    """Backtest Dual-Gate on outcome_ledger (energy/structure proxied)."""
    rows: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    print(f"\n{'='*70}")
    print(f"OUTCOME LEDGER: {len(rows)} rows from {path.name}")
    print(f"{'='*70}")

    # Phase distribution
    phase_counts: Counter[str] = Counter()
    events: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    for r in rows:
        phase = str(r.get("phase_fusion") or r.get("lifecycle_phase") or "?")
        phase_counts[phase] += 1
        events[r.get("event", "?")] += 1
        for b in (r.get("blockers") or []):
            blockers[b] += 1

    print(f"\nEvents: {dict(events)}")
    print(f"\nTop blockers: {dict(blockers.most_common(10))}")
    print(f"\nPhase distribution (n ≥ 5):")
    for phase, n in phase_counts.most_common():
        if n >= 5:
            print(f"  {phase:25s} {n:4d} ({n*100/len(rows):.1f}%)")
        else:
            others = sum(nn for p, nn in phase_counts.most_common() if nn < 5)
            print(f"  {'(other small phases)':25s} {others:4d}")
            break

    # Pre-phase candidates
    pre_phases = {"pre_pump", "pre_dump", "coil"}
    pre_rows = [r for r in rows if str(r.get("phase_fusion") or r.get("lifecycle_phase")) in pre_phases]
    mid_rows = [r for r in rows if str(r.get("phase_fusion") or r.get("lifecycle_phase")) == "mid"]

    print(f"\n{'─'*70}")
    print(f"PRE-PHASE CANDIDATES: {len(pre_rows)}")
    print(f"MID-PHASE CANDIDATES: {len(mid_rows)}")
    print(f"{'─'*70}")

    # Apply Dual-Gate counterfactual on pre-phase with proxied energy/structure
    pre_pass = 0
    pre_block = 0
    pre_by_reason: Counter[str] = Counter()
    pre_score_dist: list[float] = []
    blocked_score_dist: list[float] = []
    for r in pre_rows:
        fs = _f(r.get("fusion_score"))
        mag = magnitude_from_fusion_score(fs)
        energy = estimate_energy_from_ledger_row(r)
        structure = estimate_structure_from_ledger(r)
        ok, reason = pre_gate_decision(energy, structure, mag)
        pre_score_dist.append(fs)
        if ok:
            pre_pass += 1
        else:
            pre_block += 1
            pre_by_reason[reason.split(":")[0]] += 1
            blocked_score_dist.append(fs)

    print(f"\nDual-Gate counterfactual (proxied energy+structure):")
    print(f"  Would pass pre_gate:  {pre_pass:3d} / {len(pre_rows)} ({pre_pass*100/max(len(pre_rows),1):.1f}%)")
    print(f"  Would block:           {pre_block:3d} / {len(pre_rows)}")
    if pre_by_reason:
        print(f"  Block reasons: {dict(pre_by_reason)}")
    if pre_score_dist:
        avg = sum(pre_score_dist) / len(pre_score_dist)
        print(f"  Avg fusion_score (pass): {sum(s for s in pre_score_dist if s >= magnitude_from_fusion_score(0.15)*25)/max(len([s for s in pre_score_dist if s >= 3.75]),1):.1f}")
        if blocked_score_dist:
            print(f"  Avg fusion_score (block): {sum(blocked_score_dist)/len(blocked_score_dist):.1f}")

    # Compare with old gate blockers
    print(f"\n  Old gate blockers on pre-phase rows:")
    for b_name, b_count in Counter(
        b for r in pre_rows for b in (r.get("blockers") or [])
    ).most_common():
        print(f"    {b_name:30s} {b_count:3d}")

    # Magnitude-only counterfactual (minimum necessary condition)
    pre_above_mag_floor = sum(
        1 for r in pre_rows
        if magnitude_from_fusion_score(_f(r.get("fusion_score"))) >= PRE_GATE_MIN_MAGNITUDE
    )
    print(f"\n  Magnitude >= {PRE_GATE_MIN_MAGNITUDE} (proxy for minimum pre_gate pass):   {pre_above_mag_floor:3d} / {len(pre_rows)} ({pre_above_mag_floor*100/max(len(pre_rows),1):.1f}%)")

    # Old gate would pass on these pre-phase rows?
    old_would_pass = sum(
        1 for r in pre_rows
        if old_gate_decision(_f(r.get("fusion_score")))[0]
    )
    print(f"  Old gate (mag >= 0.5) would pass on pre-phase:            {old_would_pass:3d} / {len(pre_rows)}")

    # Energy proxy distribution
    energy_dist: Counter[int] = Counter()
    for r in pre_rows:
        energy_dist[estimate_energy_from_ledger_row(r)] += 1
    print(f"  Energy distribution (proxy): {dict(sorted(energy_dist.items()))}")

    mid_blocked_by_abs = sum(1 for r in mid_rows if "below_abs_floor" in (r.get("blockers") or []))
    mid_blocked_by_calib = sum(1 for r in mid_rows if "below_calibrated_gate" in (r.get("blockers") or []))
    print(f"\n  Mid-phase blocked by below_abs_floor:    {mid_blocked_by_abs}")
    print(f"  Mid-phase blocked by below_calibrated_gate: {mid_blocked_by_calib}")


def analyze_hunt_scan(path: Path) -> list[dict[str, Any]]:
    """Full Dual-Gate backtest on hunt_scan rows (with microstructure data).

    Returns list of pre-phase result dicts for aggregation.
    """
    rows: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    print(f"\n{'='*70}")
    print(f"HUNT SCAN: {len(rows)} rows from {path.name}")
    print(f"{'='*70}")

    pre_phases = {"pre_pump", "pre_dump", "coil"}
    results: list[dict[str, Any]] = []

    for r in rows:
        lifecycle = r.get("lifecycle") if isinstance(r.get("lifecycle"), dict) else {}
        phase = str(lifecycle.get("phase") or "")
        if phase not in pre_phases:
            continue

        market = r.get("market") if isinstance(r.get("market"), dict) else {}

        # Fusion magnitude from the active setup
        direction = lifecycle.get("bias", "long")
        setup = r.get(direction) if isinstance(r.get(direction), dict) else {}
        magnitude = _f(setup.get("magnitude"))
        fusion_score = _f(setup.get("fusion_score"))
        if magnitude <= 0:
            magnitude = magnitude_from_fusion_score(fusion_score)

        # Full Dual-Gate computation
        energy_hits = energy_from_hunt_scan(market)
        structure_score = structure_from_hunt_scan(market)
        ok, reason = pre_gate_decision(energy_hits, structure_score, magnitude)

        results.append({
            "symbol": r.get("symbol"),
            "ts": r.get("ts"),
            "phase": phase,
            "direction": direction,
            "fusion_score": fusion_score,
            "magnitude": round(magnitude, 4),
            "energy_hits": energy_hits,
            "structure_score": round(structure_score, 3),
            "depth_imbalance": market.get("depth_imbalance"),
            "oi_z": market.get("oi_z"),
            "accumulation": market.get("map_accumulation_score"),
            "absorption": market.get("map_absorption_count"),
            "pre_gate_open": ok,
            "pre_gate_reason": reason,
            "old_confirmed": bool(setup.get("confirmed")),
            "old_blockers": setup.get("gate_reason"),
        })

    passed = [r for r in results if r["pre_gate_open"]]
    blocked = [r for r in results if not r["pre_gate_open"]]

    print(f"\nPre-phase candidates: {len(results)}")
    print(f"  Would pass pre_gate: {len(passed)} ({len(passed)*100/max(len(results),1):.1f}%)")
    print(f"  Would block:          {len(blocked)} ({len(blocked)*100/max(len(results),1):.1f}%)")

    if passed:
        print(f"\n  PASS (n={len(passed)}):")
        print(f"    Avg fusion_score:   {sum(r['fusion_score'] for r in passed)/len(passed):.1f}")
        print(f"    Avg energy_hits:    {sum(r['energy_hits'] for r in passed)/len(passed):.1f}")
        print(f"    Avg structure:      {sum(r['structure_score'] for r in passed)/len(passed):.3f}")
        print(f"    Avg magnitude:      {sum(r['magnitude'] for r in passed)/len(passed):.3f}")
        if len(passed) <= 20:
            for r in passed:
                print(f"      {r['symbol']:12s} {r['phase']:10s} {r['direction']:6s} "
                      f"score={r['fusion_score']:5.1f} energy={r['energy_hits']} "
                      f"struct={r['structure_score']:.2f} mag={r['magnitude']:.3f}")

    if blocked:
        reasons: Counter[str] = Counter()
        for r in blocked:
            reasons[r["pre_gate_reason"].split(":")[0]] += 1
        print(f"\n  BLOCK (n={len(blocked)}):")
        print(f"    Reasons: {dict(reasons)}")
        print(f"    Avg fusion_score:   {sum(r['fusion_score'] for r in blocked)/len(blocked):.1f}")
        print(f"    Avg energy_hits:    {sum(r['energy_hits'] for r in blocked)/len(blocked):.1f}")
        if len(blocked) <= 10:
            for r in blocked:
                print(f"      {r['symbol']:12s} {r['phase']:10s} {r['direction']:6s} "
                      f"score={r['fusion_score']:5.1f} {r['pre_gate_reason']}")

    return results


# ── Main ──────────────────────────────────────────────────────────

def main() -> None:
    data = _data_dir()
    ledger_path = data / "hunt_outcome_ledger.jsonl"
    scan_patterns = sorted(data.glob("hunt_scan*.jsonl"))

    if ledger_path.is_file():
        analyze_outcome_ledger(ledger_path)
    else:
        print(f"[SKIP] outcome_ledger not found: {ledger_path}")

    # Aggregate scan analysis across all files
    all_pre: list[dict[str, Any]] = []
    if scan_patterns:
        print(f"\n  Found {len(scan_patterns)} scan files")
        for sp in scan_patterns:
            res = analyze_hunt_scan(sp)
            all_pre.extend(res)

        if all_pre:
            passed = [r for r in all_pre if r["pre_gate_open"]]
            blocked = [r for r in all_pre if not r["pre_gate_open"]]
            print(f"\n{'='*70}")
            print(f"AGGREGATE ({len(scan_patterns)} files, {len(all_pre)} pre-phase candidates)")
            print(f"{'='*70}")
            print(f"  Total pre-phase:     {len(all_pre)}")
            print(f"  Would pass pre_gate: {len(passed)} ({len(passed)*100/len(all_pre):.1f}%)")
            print(f"  Would block:          {len(blocked)} ({len(blocked)*100/len(all_pre):.1f}%)")
            if passed:
                print(f"  Pass energy_hits:    {sum(r['energy_hits'] for r in passed)/len(passed):.1f}")
                print(f"  Pass structure:      {sum(r['structure_score'] for r in passed)/len(passed):.3f}")
                print(f"  Pass magnitude:      {sum(r['magnitude'] for r in passed)/len(passed):.3f}")
                print(f"  Pass fusion_score:   {sum(r['fusion_score'] for r in passed)/len(passed):.1f}")
            if blocked:
                reasons: Counter[str] = Counter()
                for r in blocked:
                    reasons[r["pre_gate_reason"].split(":")[0]] += 1
                print(f"  Block reasons: {dict(reasons)}")
                print(f"  Block energy_hits:   {sum(r['energy_hits'] for r in blocked)/len(blocked):.1f}")
                print(f"  Block fusion_score:  {sum(r['fusion_score'] for r in blocked)/len(blocked):.1f}")

            # Energy distribution for all pre-phase
            energy_dist: Counter[int] = Counter()
            for r in all_pre:
                energy_dist[r["energy_hits"]] += 1
            print(f"  Energy distribution (all): {dict(sorted(energy_dist.items()))}")

            # Energy distribution for pass only
            pass_energy: Counter[int] = Counter()
            for r in passed:
                pass_energy[r["energy_hits"]] += 1
            print(f"  Energy distribution (pass): {dict(sorted(pass_energy.items()))}")
    else:
        print(f"[SKIP] hunt_scan not found in {data}")

    print()


if __name__ == "__main__":
    main()

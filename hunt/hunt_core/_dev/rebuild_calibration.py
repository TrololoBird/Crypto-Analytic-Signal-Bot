"""Rebuild hunt_calibration.json from tracker outcomes + signal history (#49)."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_core.calibration.phase_matrix import export_phase_calibration
from hunt_core.params.store import save_calibration_payload
from hunt_core.paths import HUNT_CALIBRATION, SIGNAL_HISTORY
from hunt_core.track.outcomes import genuine_closed, is_polluted, outcome_archive_key
from hunt_core.track.tracker import load_tracker_state


def load_closed_rows(path: Path = SIGNAL_HISTORY) -> list[dict[str, Any]]:
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
    return rows


def dedupe_outcome_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by entry_message_id, then outcome_archive_key (watch double-writes)."""
    seen_eid: set[int] = set()
    seen_key: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if is_polluted(row):
            continue
        eid = row.get("entry_message_id")
        if eid is not None:
            try:
                eid_int = int(eid)
            except (TypeError, ValueError):
                eid_int = None
            if eid_int is not None:
                if eid_int in seen_eid:
                    continue
                seen_eid.add(eid_int)
        else:
            key = outcome_archive_key(row)
            if key is not None:
                if key in seen_key:
                    continue
                seen_key.add(key)
        out.append(row)
    return out


def summarize_outcomes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = losses = 0
    for row in rows:
        pnl = row.get("pnl_pct")
        if pnl is None:
            continue
        try:
            pnl_f = float(pnl)
        except (TypeError, ValueError):
            continue
        if pnl_f > 0:
            wins += 1
        elif pnl_f < 0:
            losses += 1
    n = wins + losses
    wr = wins / n if n else 0.0
    return {"n": n, "wins": wins, "losses": losses, "wr": round(wr, 3)}


def _build_outcome_calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    deduped = dedupe_outcome_rows(rows)
    by_setup: dict[str, dict[str, Any]] = {}
    by_phase: dict[str, dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any]] = {}
    setup_buckets: dict[str, list[float]] = defaultdict(list)
    phase_buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    sym_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in deduped:
        sym = str(row.get("symbol") or "").upper()
        if sym:
            sym_buckets[sym].append(row)
        pnl = row.get("pnl_pct")
        if pnl is None:
            continue
        try:
            pnl_f = float(pnl)
        except (TypeError, ValueError):
            continue
        setup_id = str(
            row.get("setup_id")
            or row.get("catalog_setup")
            or row.get("setup_phase")
            or row.get("phase")
            or "unknown"
        )
        direction = str(row.get("direction") or "?")
        setup_key = f"{setup_id}:{direction}"
        setup_buckets[setup_key].append(pnl_f)
        phase = str(
            row.get("entry_lifecycle_phase")
            or row.get("setup_phase")
            or row.get("phase")
            or "?"
        )
        phase_buckets[(phase, direction)].append(pnl_f)

    for key, pnls in setup_buckets.items():
        wins = sum(1 for p in pnls if p > 0)
        n = len(pnls)
        by_setup[key] = {
            "n": n,
            "n_closed": n,
            "wr_pct": round(wins / n * 100.0, 1) if n else 0.0,
            "avg_pnl_pct": round(sum(pnls) / n, 3) if n else 0.0,
            "sl_rate": round(sum(1 for p in pnls if p < 0) / n, 3) if n else 0.0,
        }

    for (phase, direction), pnls in phase_buckets.items():
        wins = sum(1 for p in pnls if p > 0)
        n = len(pnls)
        by_phase[f"{phase}:{direction}"] = {
            "n": n,
            "wr_pct": round(wins / n * 100.0, 1) if n else 0.0,
            "avg_pnl_pct": round(sum(pnls) / n, 3) if n else 0.0,
        }

    for sym, sym_rows in sym_buckets.items():
        by_symbol[sym] = {
            **summarize_outcomes(sym_rows),
            "by_setup": _setup_stats_for_rows(sym_rows),
        }

    return {
        "deduped_n": len(deduped),
        "global": summarize_outcomes(deduped),
        "by_setup": by_setup,
        "by_phase_direction": by_phase,
        "by_symbol": by_symbol,
    }


def _setup_stats_for_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        pnl = row.get("pnl_pct")
        if pnl is None:
            continue
        setup_id = str(
            row.get("setup_id")
            or row.get("catalog_setup")
            or row.get("phase")
            or "unknown"
        )
        direction = str(row.get("direction") or "?")
        try:
            buckets[f"{setup_id}:{direction}"].append(float(pnl))
        except (TypeError, ValueError):
            continue
    out: dict[str, dict[str, Any]] = {}
    for key, pnls in buckets.items():
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        out[key] = {
            "n": n,
            "n_closed": n,
            "wr_pct": round(wins / n * 100.0, 1) if n else 0.0,
            "sl_rate": round(sum(1 for p in pnls if p < 0) / n, 3) if n else 0.0,
        }
    return out


def _tracker_closed_summary() -> dict[str, Any]:
    state = load_tracker_state()
    signals = state.get("signals") or {}
    closed = [
        sig
        for sig in signals.values()
        if isinstance(sig, dict) and sig.get("status") == "closed"
    ]
    history = state.get("closed_history") or []
    if isinstance(history, list):
        closed.extend(sig for sig in history if isinstance(sig, dict))
    return summarize_outcomes(genuine_closed(closed))


def build_calibration_report(cal: dict[str, Any] | None = None) -> dict[str, Any]:
    """Per-setup lake report — WR, avg PnL, flip eligibility (T4)."""
    from hunt_core.setups.catalog import HUNT_SETUP_IDS, setup_ev_flip_eligible

    payload = cal if cal is not None else rebuild_calibration(dry_run=True)
    oc = (payload.get("outcome_calibration") or {}).get("by_setup") or {}
    if not isinstance(oc, dict):
        oc = {}
    setups: list[dict[str, Any]] = []
    for setup_id in HUNT_SETUP_IDS:
        for direction in ("short", "long"):
            key = f"{setup_id}:{direction}"
            row = oc.get(key) if isinstance(oc.get(key), dict) else {}
            n = int(row.get("n") or row.get("n_closed") or 0)
            setups.append(
                {
                    "setup_id": setup_id,
                    "direction": direction,
                    "n": n,
                    "wr_pct": row.get("wr_pct"),
                    "avg_pnl_pct": row.get("avg_pnl_pct"),
                    "sl_rate": row.get("sl_rate"),
                    "lake_flip_eligible": setup_ev_flip_eligible(
                        {"by_setup": oc},
                        setup_id=setup_id,
                        direction=direction,
                    ),
                }
            )
    setups.sort(key=lambda r: (-int(r["n"]), str(r["setup_id"])))
    global_stats = (payload.get("outcome_calibration") or {}).get("global") or {}
    return {
        "computed_at": payload.get("computed_at"),
        "version": payload.get("version"),
        "deduped_n": (payload.get("outcome_calibration") or {}).get("deduped_n"),
        "global": global_stats,
        "setups": setups,
        "flip_ready": [s for s in setups if s.get("lake_flip_eligible")],
    }


def write_calibration_report(
    path: Path | None = None,
    *,
    cal: dict[str, Any] | None = None,
) -> Path:
    """Write JSON report to hunt/data/calibration_report.json."""
    from hunt_core.paths import DATA

    report = build_calibration_report(cal)
    out_path = path or (DATA / "calibration_report.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out_path


def rebuild_calibration(*, dry_run: bool = False) -> dict[str, Any]:
    history_rows = load_closed_rows()
    history_stats = summarize_outcomes(dedupe_outcome_rows(history_rows))
    tracker_stats = _tracker_closed_summary()
    phase_matrix = export_phase_calibration()
    all_rows = list(history_rows)
    state = load_tracker_state()
    for sig in (state.get("signals") or {}).values():
        if isinstance(sig, dict) and sig.get("status") == "closed":
            all_rows.append(sig)
    for sig in state.get("closed_history") or []:
        if isinstance(sig, dict):
            all_rows.append(sig)
    outcome_calibration = _build_outcome_calibration(all_rows)
    cal_path = HUNT_CALIBRATION
    existing: dict[str, Any] = {}
    if cal_path.is_file():
        try:
            existing = json.loads(cal_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
    out = {
        **existing,
        "version": int(existing.get("version") or 1) + 1,
        "computed_at": datetime.now(UTC).isoformat(),
        "outcome_stats": tracker_stats,
        "signal_history_stats": history_stats,
        "outcome_calibration": outcome_calibration,
        "phase_matrix": phase_matrix,
        "global": existing.get("global") or {},
    }
    if not dry_run:
        save_calibration_payload(out, path=cal_path)
        write_calibration_report(cal=out)
    return out


if __name__ == "__main__":
    import argparse
    import pprint

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    if args.report_only:
        path = write_calibration_report()
        print(f"wrote {path}")
        pprint.pp(build_calibration_report())
    else:
        from hunt_core.paths import DATA

        cal = rebuild_calibration(dry_run=args.dry_run)
        pprint.pp(cal.get("outcome_calibration", {}).get("global"))
        if not args.dry_run:
            print(f"report: {DATA / 'calibration_report.json'}")

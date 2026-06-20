"""Replay tick snapshots through current scoring + delivery gates."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hunt_core.paths import LAKE_PARQUET, TICK_JSONL


def _impulse_bounds(row: dict[str, Any]) -> tuple[float, float]:
    imp = row.get("impulse") or {}
    hunt_h = float(row.get("impulse_high") or imp.get("hunt_high") or 0)
    hunt_l = float(row.get("impulse_low") or imp.get("hunt_low") or 0)
    return hunt_h, hunt_l


def recompute_tick_row(row: dict[str, Any]) -> dict[str, Any]:
    """Hydrate JSONL row for delivery replay (lifecycle + MTF dict)."""
    from hunt_core.runtime.tick_jsonl import hydrate_tick_row_from_jsonl

    out = hydrate_tick_row_from_jsonl(dict(row))
    out["recomputed"] = True
    out["recompute_note"] = "jsonl_hydrate"
    return out


def find_tick_row(
    symbol: str,
    *,
    path: Path = TICK_JSONL,
    confirmed_only: bool = False,
    tail_bytes: int = 3_000_000,
) -> dict[str, Any] | None:
    sym = str(symbol or "").upper()
    if not sym or not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line or sym not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or str(row.get("symbol") or "").upper() != sym:
            continue
        if confirmed_only:
            dump = row.get("dump") or {}
            long_s = row.get("long") or {}
            if not (dump.get("confirmed") or long_s.get("confirmed")):
                continue
        if row.get("error"):
            continue
        if not row.get("timeframes"):
            continue
        row = dict(row)
        row["tick_path"] = str(path)
        from hunt_core.runtime.tick_jsonl import hydrate_tick_row_from_jsonl

        return hydrate_tick_row_from_jsonl(row)
    return None


def delivery_replay_report(
    row: dict[str, Any],
    *,
    direction: str = "short",
    recompute: bool = False,
) -> dict[str, Any]:
    from hunt_core.deliver.dispatch import evaluate_delivery, evaluate_delivery_fast
    from hunt_core.gate.delivery import collect_report_blockers
    from hunt_core.runtime.cycle._cycle_advisory import early_telegram_block_reason
    from hunt_core.track.events import audit_probe_row

    if recompute:
        row = recompute_tick_row(row)
    sym = str(row.get("symbol") or "").upper()
    lc = row.get("lifecycle") or {}
    direction = direction.lower()
    setup = (row.get("dump") if direction == "short" else row.get("long")) or {}
    from hunt_core.gate._ev import setup_conviction_pct, setup_p_win

    use_fast = row.get("tick_path") in {
        "hot_ws",
        "hot_bootstrap",
        "hot_delta",
        "hot_carry",
    } or bool(row.get("recomputed"))
    eval_fn = evaluate_delivery_fast if use_fast else evaluate_delivery
    audit = audit_probe_row(row, source="replay")
    out: dict[str, Any] = {
        "symbol": sym,
        "row_ts": row.get("ts"),
        "recomputed": bool(row.get("recomputed")),
        "direction": direction,
        "confirmed": bool(setup.get("confirmed")),
        "p_win": setup_p_win(setup),
        "conviction": setup_conviction_pct(setup, direction=direction),
        "fuel": setup.get("dump_fuel" if direction == "short" else "long_fuel"),
        "fuel_before_cap": setup.get("fuel_before_structure_cap"),
        "structure_ev_cap": setup.get("structure_ev_cap"),
        "ev_shadow": (setup.get("ev_shadow") or {}).get("ev"),
        "rr": setup.get("risk_reward"),
        "levels_viable": setup.get("levels_viable"),
        "levels_veto": setup.get("levels_veto"),
        "phase": setup.get("phase"),
        "lc_phase": lc.get("phase"),
        "lc_bias": lc.get("recommended_bias"),
        "audit_ok": audit.get("ok"),
        "audit_issues": audit.get("issues"),
    }
    if not setup.get("confirmed"):
        out["early_block"] = early_telegram_block_reason(
            setup,
            direction=direction,
            lifecycle=lc,
            row=row,
        )
        return out
    gate, tier = eval_fn(row, direction=direction, setup=setup, lifecycle=lc, symbol=sym)
    blockers = collect_report_blockers(
        setup, direction=direction, symbol=sym, lifecycle=lc, row=row
    )
    out["delivery_ok"] = gate.ok
    out["gate_code"] = gate.code
    out["gate_message"] = gate.message
    out["tier"] = tier
    out["blocker_codes"] = [b.code for b in blockers if not b.ok]
    out["early_block"] = early_telegram_block_reason(
        setup,
        direction=direction,
        lifecycle=lc,
        row=row,
    )
    return out


def batch_delivery_replay(
    rows: list[dict[str, Any]],
    *,
    direction: str = "short",
    recompute: bool = False,
) -> dict[str, Any]:
    """Phase 6 harness: replay delivery decisions for many tick rows."""
    reports = [
        delivery_replay_report(row, direction=direction, recompute=recompute)
        for row in rows
        if isinstance(row, dict)
    ]
    return _summarize_replay_reports(reports)


def _summarize_replay_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    ok_n = sum(1 for r in reports if r.get("delivery_ok"))
    ev_vals = [float(r["ev_shadow"]) for r in reports if r.get("ev_shadow") is not None]
    return {
        "n": len(reports),
        "delivery_ok": ok_n,
        "delivery_blocked": len(reports) - ok_n,
        "ev_shadow_mean": round(sum(ev_vals) / len(ev_vals), 4) if ev_vals else None,
        "ev_shadow_negative": sum(1 for v in ev_vals if v < 0),
        "block_codes": _count_codes(reports, "gate_code"),
        "blocker_codes": _count_codes(reports, "blocker_codes"),
        "per_setup_wr": per_setup_wr_stub(reports),
        "reports": reports,
    }


def per_setup_wr_stub(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Stub WR bucket by lifecycle phase — calibration placeholder."""
    buckets: dict[str, dict[str, int]] = {}
    for rep in reports:
        phase = str(rep.get("lc_phase") or rep.get("phase") or "unknown")
        b = buckets.setdefault(phase, {"n": 0, "delivery_ok": 0})
        b["n"] += 1
        if rep.get("delivery_ok"):
            b["delivery_ok"] += 1
    out: dict[str, dict[str, Any]] = {}
    for phase, counts in buckets.items():
        n = counts["n"]
        ok = counts["delivery_ok"]
        out[phase] = {
            "n": n,
            "delivery_ok": ok,
            "wr_stub": round(ok / n, 3) if n else None,
        }
    return out


def compare_shadow_live(
    row: dict[str, Any],
    *,
    direction: str = "short",
) -> dict[str, Any]:
    """Compare stored tick metrics vs recomputed shadow path."""
    live = delivery_replay_report(row, direction=direction, recompute=False)
    shadow = delivery_replay_report(row, direction=direction, recompute=True)
    deltas: dict[str, Any] = {}
    for key in ("confirmed", "p_win", "conviction", "fuel", "rr", "delivery_ok", "gate_code", "phase"):
        if live.get(key) != shadow.get(key):
            deltas[key] = {"live": live.get(key), "shadow": shadow.get(key)}
    return {
        "symbol": live.get("symbol"),
        "direction": direction,
        "live": live,
        "shadow": shadow,
        "deltas": deltas,
        "blocker_hist_live": _count_codes([live], "blocker_codes"),
        "blocker_hist_shadow": _count_codes([shadow], "blocker_codes"),
    }


def vectorized_replay(
    rows: list[dict[str, Any]],
    *,
    direction: str = "short",
    compare_shadow: bool = True,
) -> dict[str, Any]:
    """Vectorized-ish replay over lake rows with optional shadow/live diffs."""
    summary = batch_delivery_replay(rows, direction=direction, recompute=False)
    if compare_shadow:
        shadow_summary = batch_delivery_replay(rows, direction=direction, recompute=True)
        drift_n = 0
        drift_samples: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cmp = compare_shadow_live(row, direction=direction)
            if cmp.get("deltas"):
                drift_n += 1
                if len(drift_samples) < 20:
                    drift_samples.append(cmp)
        summary["shadow_recompute"] = {
            k: v
            for k, v in shadow_summary.items()
            if k != "reports"
        }
        summary["shadow_drift_n"] = drift_n
        summary["shadow_drift_samples"] = drift_samples
    return summary


def _count_codes(reports: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rep in reports:
        val = rep.get(key)
        if isinstance(val, list):
            for code in val:
                counts[str(code)] = counts.get(str(code), 0) + 1
        elif val:
            counts[str(val)] = counts.get(str(val), 0) + 1
    return counts


__all__ = [
    "batch_delivery_replay",
    "compare_shadow_live",
    "delivery_replay_report",
    "find_tick_row",
    "load_replay_rows",
    "per_setup_wr_stub",
    "recompute_tick_row",
    "vectorized_replay",
]


def _load_jsonl_rows(path: Path, *, limit: int = 500) -> list[dict[str, Any]]:
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
            if len(rows) >= limit:
                break
    return rows


def _load_parquet_rows(path: Path, *, limit: int = 500) -> list[dict[str, Any]]:
    """Load tick rows from lake parquet (row_json column or struct columns)."""
    try:
        import polars as pl
    except ImportError:
        return []
    try:
        df = pl.read_parquet(path)
    except OSError:
        return []
    if df.is_empty():
        return []
    out: list[dict[str, Any]] = []
    if "row_json" in df.columns:
        for raw in df["row_json"].head(limit).to_list():
            if isinstance(raw, dict):
                out.append(raw)
                continue
            if not raw:
                continue
            try:
                out.append(json.loads(str(raw)))
            except json.JSONDecodeError:
                continue
        return out
    for row in df.head(limit).iter_rows(named=True):
        if isinstance(row, dict):
            out.append(row)
    return out


def load_replay_rows(
    path: Path | None = None,
    *,
    limit: int = 500,
    prefer_parquet: bool = False,
) -> list[dict[str, Any]]:
    """Phase 6: JSONL tick archive with optional lake parquet fallback."""
    if path is None:
        path = TICK_JSONL
    if path.suffix == ".parquet" and path.is_file():
        return _load_parquet_rows(path, limit=limit)
    if path.is_dir():
        parts = sorted(path.glob("**/*.parquet"), key=lambda p: p.stat().st_mtime, reverse=True)
        if parts:
            return _load_parquet_rows(parts[0], limit=limit)
    rows = _load_jsonl_rows(path, limit=limit) if path.is_file() else []
    if rows or not prefer_parquet:
        return rows
    if LAKE_PARQUET.is_dir():
        parts = sorted(LAKE_PARQUET.glob("**/*.parquet"), key=lambda p: p.stat().st_mtime, reverse=True)
        if parts:
            return _load_parquet_rows(parts[0], limit=limit)
    return rows


if __name__ == "__main__":
    import argparse
    import pprint

    ap = argparse.ArgumentParser(description="Replay delivery gates on tick JSONL")
    ap.add_argument("--path", type=Path, default=TICK_JSONL)
    ap.add_argument("--direction", default="short", choices=("short", "long"))
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--parquet", action="store_true", help="Fall back to hunt/data/lake/parquet")
    ap.add_argument("--recompute", action="store_true")
    ap.add_argument("--vectorized", action="store_true", help="Shadow vs live drift summary")
    args = ap.parse_args()
    rows = load_replay_rows(args.path, limit=args.limit, prefer_parquet=args.parquet)
    if args.vectorized:
        summary = vectorized_replay(rows, direction=args.direction)
    else:
        summary = batch_delivery_replay(
            rows, direction=args.direction, recompute=args.recompute
        )
    pprint.pp({k: v for k, v in summary.items() if k not in {"reports", "shadow_drift_samples"}})

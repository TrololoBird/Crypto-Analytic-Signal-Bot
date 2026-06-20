"""Calibration probe — forward liquidation zones vs realized WS cascades + sticky walls."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from hunt_core.maps.liquidation import calibration_confidence
from hunt_core.params.store import save_maps_calibration
from hunt_core.paths import TICK_JSONL


def _load_ticks(path: Path, *, symbol: str | None, limit: int) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if symbol and str(row.get("symbol", "")).upper() != symbol.upper():
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _sticky_reaction_score(row: dict) -> float:
    """Heuristic: sticky wall near price + subsequent cascade or footprint stack."""
    market = row.get("market") or {}
    maps = row.get("maps") or {}
    ob = maps.get("orderbook") if isinstance(maps, dict) else {}
    ob_sticky = ob.get("sticky_walls") if isinstance(ob, dict) else None
    sticky_n = int(
        market.get("map_sticky_wall_count")
        or (len(ob_sticky) if isinstance(ob_sticky, list) else 0)
        or 0
    )
    if sticky_n < 1:
        return 0.0
    score = 0.25
    if market.get("liq_cascade_risk"):
        score += 0.35
    if market.get("map_stacked_imbalance"):
        score += 0.2
    if market.get("map_absorption_count", 0) or (ob or {}).get("absorption_zones"):
        score += 0.2
    return min(1.0, score)


def score_tick(row: dict) -> dict:
    market = row.get("market") or {}
    maps = row.get("maps") or {}
    liq = (maps.get("liquidation") or {}) if isinstance(maps, dict) else {}
    forward = liq.get("liq_forward_zones") or market.get("liq_forward_zones") or []
    if isinstance(liq, dict) and liq.get("forward_zones"):
        forward = liq.get("forward_zones")
    realized = liq.get("liq_realized_zones") or []
    if not realized:
        zones = market.get("liq_density_zones") or []
        realized = [z for z in zones if isinstance(z, dict) and int(z.get("event_count") or 0) > 0]
    conf = calibration_confidence(forward, realized)
    ob = (maps.get("orderbook") or {}) if isinstance(maps, dict) else {}
    sticky = len(ob.get("sticky_walls") or []) if isinstance(ob, dict) else 0
    return {
        "symbol": row.get("symbol"),
        "ts": row.get("ts"),
        "forward_confidence": conf,
        "forward_zones": len(forward),
        "realized_zones": len(realized),
        "sticky_walls": sticky,
        "sticky_reaction_score": round(_sticky_reaction_score(row), 3),
        "cascade": market.get("liq_cascade_risk"),
    }


def _aggregate_calibration(scores: list[dict]) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    by_sym: dict[str, list[float]] = defaultdict(list)
    sticky_by_sym: dict[str, list[float]] = defaultdict(list)
    for s in scores:
        sym = str(s.get("symbol") or "").upper()
        if not sym:
            continue
        by_sym[sym].append(float(s["forward_confidence"]))
        sticky_by_sym[sym].append(float(s["sticky_reaction_score"]))
    per_symbol: dict[str, dict[str, float]] = {}
    for sym, confs in by_sym.items():
        if not confs:
            continue
        per_symbol[sym] = {
            "calibrated_forward_confidence": round(sum(confs) / len(confs), 3),
            "sticky_reaction_min": round(sum(sticky_by_sym[sym]) / len(sticky_by_sym[sym]), 3),
        }
    universal: dict[str, float] = {}
    if scores:
        universal["calibrated_forward_confidence"] = round(
            sum(float(s["forward_confidence"]) for s in scores) / len(scores), 3
        )
        universal["sticky_reaction_min"] = round(
            sum(float(s["sticky_reaction_score"]) for s in scores) / len(scores), 3
        )
    return universal, per_symbol


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maps calibration report from tick JSONL")
    parser.add_argument("--symbol", default=None, help="Filter symbol e.g. BTCUSDT")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--path", type=Path, default=TICK_JSONL)
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Write aggregated forward confidence into hunt_calibration.json",
    )
    args = parser.parse_args(argv)
    ticks = _load_ticks(args.path, symbol=args.symbol, limit=args.limit)
    if not ticks:
        print(f"No ticks in {args.path}")
        return 1
    scores = [score_tick(r) for r in ticks]
    avg_conf = sum(s["forward_confidence"] for s in scores) / len(scores)
    sticky_hits = sum(1 for s in scores if s["sticky_walls"] >= 2)
    sticky_react = sum(s["sticky_reaction_score"] for s in scores) / len(scores)
    cascade_hits = sum(1 for s in scores if s["cascade"])
    print("=== Maps calibration probe ===")
    print(f"Ticks: {len(scores)}")
    print(f"Avg forward/realized overlap confidence: {avg_conf:.3f}")
    print(f"Avg sticky-wall reaction score: {sticky_react:.3f}")
    print(f"Ticks with 2+ sticky walls: {sticky_hits}")
    print(f"Ticks with cascade risk: {cascade_hits}")
    if args.persist:
        universal, per_symbol = _aggregate_calibration(scores)
        save_maps_calibration(universal=universal, per_symbol=per_symbol)
        print(f"Persisted calibration for {len(per_symbol)} symbols + universal")
    print("\nSample (last 5):")
    for s in scores[-5:]:
        print(json.dumps(s, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

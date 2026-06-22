"""Trade plan builder."""
from __future__ import annotations

from typing import Any

from hunt_core.deep.plan import validate_plan_geometry
from hunt_core.deep.verdict_v2._helpers import atr_from_row, rr_ratio, safe_float
from hunt_core.deep.verdict_v2.config import TradePlanConfig
from hunt_core.deep.verdict_v2.levels import entry_zone, pick_stop, pick_targets
from hunt_core.deep.verdict_v2.types import ExpectedPath, TradePlan


def build_trade_plan(
    row: dict[str, Any],
    path: ExpectedPath,
    cfg: TradePlanConfig,
) -> TradePlan | None:
    if path.direction not in {"long", "short"}:
        return None
    direction = path.direction
    zone, zone_src = entry_zone(row, direction, cfg.entry_atr_pad)
    price = safe_float(row.get("price"))
    atr = atr_from_row(row)
    zone_mid = (zone[0] + zone[1]) / 2.0
    entry_type = "pullback_limit" if price > 0 and abs(price - zone_mid) > atr * 0.12 else "market"
    entry = price if entry_type == "market" and price > 0 else zone_mid
    stop, stop_src = pick_stop(row, direction, entry)
    targets, tgt_factors = pick_targets(row, direction)

    if not targets:
        if direction == "long":
            targets = [entry + atr * 2, entry + atr * 3.5, entry + atr * 5]
        else:
            targets = [entry - atr * 2, entry - atr * 3.5, entry - atr * 5]
        tgt_factors = ["atr_fallback"]

    if direction == "long":
        targets = sorted(t for t in targets if t > entry * 1.0005)[:3]
    else:
        targets = sorted((t for t in targets if t < entry * 0.9995), reverse=True)[:3]
    while len(targets) < 3:
        mult = (len(targets) + 1) * 2
        targets.append(entry + atr * mult if direction == "long" else entry - atr * mult)

    tp1, tp2, tp3 = targets[0], targets[1], targets[2]
    rr1 = rr_ratio(entry, tp1, stop, direction)
    if rr1 < cfg.min_rr_tp1 and atr > 0:
        if direction == "long":
            tp1 = entry + max(atr * 2.0, (tp1 - entry) * 1.5)
        else:
            tp1 = entry - max(atr * 2.0, (entry - tp1) * 1.5)
        rr1 = rr_ratio(entry, tp1, stop, direction)
    # Re-sort after possible tp1 adjustment so the ladder stays monotonic
    targets = (
        sorted([tp1, tp2, tp3]) if direction == "long" else sorted([tp1, tp2, tp3], reverse=True)
    )
    tp1, tp2, tp3 = targets[0], targets[1], targets[2]
    # Use zone width as ATR proxy if atr_from_row returned 0 — avoids 1.0 fallback
    # blowing SL correction to +1.2 price units on low-price symbols.
    zone_width = abs(zone[1] - zone[0])
    atr_for_geom = atr if atr and atr > 0 else max(zone_width * 2.0, 1e-6)
    # Geometry validation: SL outside zone, monotonic TPs, no 0R/absurd R
    geom = validate_plan_geometry(
        {
            "entry_zone": [zone[0], zone[1]],
            "stop_loss": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
        },
        direction=direction,
        atr=atr_for_geom,
    )
    stop = float(geom.get("stop_loss") or stop)
    tp1 = float(geom.get("tp1") or tp1)
    tp2 = float(geom.get("tp2") or tp2)
    tp3 = float(geom.get("tp3") or tp3)
    rr2 = rr_ratio(entry, tp2, stop, direction)
    rr3 = rr_ratio(entry, tp3, stop, direction)
    rr1 = rr_ratio(entry, tp1, stop, direction)
    sources = [zone_src, stop_src, *tgt_factors[:2]]
    return TradePlan(
        direction=direction,  # type: ignore[arg-type]
        entry_type=entry_type,  # type: ignore[arg-type]
        entry_zone=(round(zone[0], 6), round(zone[1], 6)),
        stop_loss=round(stop, 6),
        take_profit_1=round(tp1, 6),
        take_profit_2=round(tp2, 6),
        take_profit_3=round(tp3, 6),
        rr_tp1=round(rr1, 2),
        rr_tp2=round(rr2, 2),
        rr_tp3=round(rr3, 2),
        rr_primary=round(rr1, 2),
        invalidation_reason=f"Close beyond {stop_src}",
        level_sources=sources,
    )

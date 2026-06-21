"""Trade plan builder."""
from __future__ import annotations

from typing import Any

from hunt_core.analysis.deep.verdict_v2._helpers import atr_from_row, rr_ratio, safe_float
from hunt_core.analysis.deep.verdict_v2.config import TradePlanConfig
from hunt_core.analysis.deep.verdict_v2.levels import entry_zone, pick_stop, pick_targets
from hunt_core.analysis.deep.verdict_v2.types import ExpectedPath, TradePlan


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
            targets[0] = tp1
        else:
            tp1 = entry - max(atr * 2.0, (entry - tp1) * 1.5)
            targets[0] = tp1
        rr1 = rr_ratio(entry, tp1, stop, direction)
    tp2, tp3 = targets[1], targets[2]
    rr2 = rr_ratio(entry, tp2, stop, direction)
    rr3 = rr_ratio(entry, tp3, stop, direction)
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

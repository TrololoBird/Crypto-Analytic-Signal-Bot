"""Trade plan builder."""
from __future__ import annotations

from typing import Any

from hunt_core.deep.plan import finalize_plan_geometry
from hunt_core.deep.verdict_v2._helpers import atr_from_row, safe_float
from hunt_core.deep.verdict_v2.activation import assess_activation, plan_lifecycle_from_activation, recompute_plan_on_activation
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
    from hunt_core.deep.verdict_v2.levels import pick_catalyst_level

    cat_level, _ = pick_catalyst_level(row, direction, entry_zone=zone, atr=atr)
    stop, stop_src = pick_stop(row, direction, zone_mid, catalyst_level=cat_level)
    targets, tgt_factors = pick_targets(row, direction)

    if not targets:
        if direction == "long":
            targets = [zone_mid + atr * 2, zone_mid + atr * 3.5, zone_mid + atr * 5]
        else:
            targets = [zone_mid - atr * 2, zone_mid - atr * 3.5, zone_mid - atr * 5]
        tgt_factors = ["atr_fallback"]

    if direction == "long":
        targets = sorted(t for t in targets if t > zone_mid * 1.0005)[:3]
    else:
        targets = sorted((t for t in targets if t < zone_mid * 0.9995), reverse=True)[:3]
    while len(targets) < 3:
        mult = (len(targets) + 1) * 2
        targets.append(zone_mid + atr * mult if direction == "long" else zone_mid - atr * mult)

    zone_width = abs(zone[1] - zone[0])
    atr_for_geom = atr if atr and atr > 0 else max(zone_width * 2.0, 1e-6)
    geom = finalize_plan_geometry(
        {
            "entry_zone": [zone[0], zone[1]],
            "stop_loss": stop,
            "tp1": targets[0],
            "tp2": targets[1],
            "tp3": targets[2],
            "price_hint": price,
        },
        direction=direction,
        atr=atr_for_geom,
    )
    zone = (round(float(geom["entry_zone"][0]), 6), round(float(geom["entry_zone"][1]), 6))
    stop = float(geom.get("stop_loss") or stop)
    tp1 = float(geom.get("tp1") or targets[0])
    tp2 = float(geom.get("tp2") or targets[1])
    tp3 = float(geom.get("tp3") or targets[2])
    # entry_reference from geometry is the zone midpoint — use it for both directions.
    _geom_entry_ref = geom.get("entry_reference")
    entry_ref = float(_geom_entry_ref if _geom_entry_ref else (zone[0] if direction == "long" else zone[1]))
    rr1 = float(geom.get("rr_tp1") or 0)
    rr2 = float(geom.get("rr_tp2") or 0)
    rr3 = float(geom.get("rr_tp3") or 0)
    sources = [zone_src, stop_src, *tgt_factors[:2]]

    summary_stub = {
        "entry_lo": zone[0],
        "entry_hi": zone[1],
        "entry_type": entry_type,
    }
    act_state = str(assess_activation(row, summary_stub, entry_type=entry_type).get("state") or "idle")
    lifecycle = plan_lifecycle_from_activation(act_state)

    plan = TradePlan(
        direction=direction,  # type: ignore[arg-type]
        entry_type=entry_type,  # type: ignore[arg-type]
        entry_zone=zone,
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
        entry_reference=round(entry_ref, 6),
        rr_base_label=str(geom.get("rr_base_label") or "≈R:R (от края зоны)"),
        plan_lifecycle=lifecycle,
    )
    if lifecycle == "active" and price > 0:
        plan = recompute_plan_on_activation(plan, fill_price=price)
    return plan

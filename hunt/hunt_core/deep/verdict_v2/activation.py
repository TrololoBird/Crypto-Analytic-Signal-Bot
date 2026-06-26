"""Activation lifecycle — forming → armed → active (R4)."""
from __future__ import annotations

from typing import Any, Literal

from hunt_core.deep.verdict_v2._helpers import rr_ratio, safe_float
from hunt_core.deep.verdict_v2.types import PlanLifecycle, TradePlan

ActivationState = Literal["idle", "near_entry", "in_entry_zone", "near_catalyst", "at_catalyst"]


def assess_activation(
    row: dict[str, Any],
    summary: dict[str, Any],
    *,
    entry_type: str | None = None,
) -> dict[str, Any]:
    price = safe_float(row.get("price"))
    if price <= 0:
        return {"state": "idle", "dist_catalyst_pct": None, "dist_entry_pct": None, "detail": ""}

    state: ActivationState = "idle"
    dist_cat: float | None = None
    dist_entry: float | None = None
    et = str(entry_type or summary.get("entry_type") or "")

    cat = summary.get("catalyst_level")
    if cat is not None:
        try:
            cp = float(cat)
            dist_cat = abs(price - cp) / price * 100.0
            if dist_cat <= 0.15:
                state = "at_catalyst"
            elif dist_cat <= 0.55 and state == "idle":
                state = "near_catalyst"
        except (TypeError, ValueError):
            pass

    lo = summary.get("entry_lo")
    hi = summary.get("entry_hi")
    try:
        el, eh = float(lo), float(hi)
        if el > 0 and eh > 0:
            zone_mid = (el + eh) / 2.0
            at_resistance = False
            struct = row.get("structure") if isinstance(row.get("structure"), dict) else {}
            kl = struct.get("key_levels") if isinstance(struct.get("key_levels"), dict) else {}
            resist = safe_float(kl.get("resistance") or kl.get("last_swing_high"))
            if resist > 0 and price >= resist * 0.997:
                at_resistance = True
            if el <= price <= eh:
                if et == "pullback_limit" and (price > zone_mid or at_resistance):
                    state = "near_entry"
                    dist_entry = min(abs(price - el), abs(price - eh)) / price * 100.0
                else:
                    state = "in_entry_zone"
            else:
                dist_entry = min(abs(price - el), abs(price - eh)) / price * 100.0
                if dist_entry <= 0.35 and state in {"idle", "near_catalyst"}:
                    state = "near_entry"
    except (TypeError, ValueError):
        pass

    detail = ""
    if state == "in_entry_zone":
        detail = "active"
    elif state in {"near_entry", "at_catalyst", "near_catalyst"}:
        detail = "armed"

    return {
        "state": state,
        "dist_catalyst_pct": round(dist_cat, 3) if dist_cat is not None else None,
        "dist_entry_pct": round(dist_entry, 3) if dist_entry is not None else None,
        "detail": detail,
    }


def plan_lifecycle_from_activation(activation_state: str) -> PlanLifecycle:
    if activation_state == "in_entry_zone":
        return "active"
    if activation_state in {"near_entry", "near_catalyst", "at_catalyst"}:
        return "armed"
    return "forming"


def recompute_plan_on_activation(
    plan: TradePlan,
    *,
    fill_price: float,
) -> TradePlan:
    """Recompute R/targets from actual fill reference when signal becomes active."""
    if fill_price <= 0:
        return plan
    direction = plan.direction
    stop = plan.stop_loss
    tp1, tp2, tp3 = plan.take_profit_1, plan.take_profit_2, plan.take_profit_3
    rr1 = rr_ratio(fill_price, tp1, stop, direction)
    rr2 = rr_ratio(fill_price, tp2, stop, direction)
    rr3 = rr_ratio(fill_price, tp3, stop, direction)
    return TradePlan(
        direction=direction,
        entry_type=plan.entry_type,
        entry_zone=plan.entry_zone,
        stop_loss=plan.stop_loss,
        take_profit_1=tp1,
        take_profit_2=tp2,
        take_profit_3=tp3,
        rr_tp1=round(rr1, 2),
        rr_tp2=round(rr2, 2),
        rr_tp3=round(rr3, 2),
        rr_primary=round(rr1, 2),
        invalidation_reason=plan.invalidation_reason,
        level_sources=plan.level_sources,
        entry_reference=round(fill_price, 6),
        rr_conservative_tp1=plan.rr_conservative_tp1,
        rr_conservative_tp2=plan.rr_conservative_tp2,
        rr_conservative_tp3=plan.rr_conservative_tp3,
        rr_base_label="R:R (от входа)",
        plan_lifecycle="active",
    )


def activation_event(
    row: dict[str, Any],
    plan: TradePlan | None,
    summary: dict[str, Any],
    *,
    prev_lifecycle: str = "",
) -> dict[str, Any] | None:
    """One-shot activation event when transitioning into the entry zone."""
    if plan is None:
        return None
    act = assess_activation(row, summary)
    lifecycle = plan_lifecycle_from_activation(str(act.get("state") or "idle"))
    if lifecycle != "active" or prev_lifecycle == "active":
        return None
    price = safe_float(row.get("price"))
    if price <= 0:
        return None
    recomputed = recompute_plan_on_activation(plan, fill_price=price)
    return {
        "event": "plan_activated",
        "symbol": str(row.get("symbol") or "").upper(),
        "fill_reference": price,
        "rr_tp1": recomputed.rr_tp1,
        "rr_tp2": recomputed.rr_tp2,
        "rr_tp3": recomputed.rr_tp3,
        "rr_base_label": recomputed.rr_base_label,
    }


__all__ = [
    "ActivationState",
    "activation_event",
    "assess_activation",
    "plan_lifecycle_from_activation",
    "recompute_plan_on_activation",
]

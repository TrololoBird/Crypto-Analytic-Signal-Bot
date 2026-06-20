"""Map-derived gate checks — opposing liquidity bias veto."""
from __future__ import annotations

from typing import Any


def map_opposing_bias_veto(
    row: dict[str, Any],
    *,
    direction: str,
) -> tuple[bool, str | None]:
    """Veto when professional maps strongly oppose the intended direction."""
    market = row.get("market") or {}
    if not isinstance(market, dict):
        return False, None
    d = direction.lower().strip()
    cascade = market.get("liq_cascade_risk")
    stacked = market.get("map_stacked_imbalance")
    sticky_side = market.get("map_nearest_sticky_side")
    fwd_conf = float(market.get("liq_forward_confidence") or 1.0)
    fwd_weight = float(market.get("liq_forward_weight") or 0.35)

    if d == "short":
        if cascade == "short_squeeze" and fwd_conf > 0.5:
            return True, "map_veto_short_squeeze_risk"
        if stacked == "buy_stack":
            return True, "map_veto_buy_stack_imbalance"
        if sticky_side == "bid" and market.get("map_nearest_sticky_wall_pct") is not None:
            if float(market["map_nearest_sticky_wall_pct"]) <= 0.8:
                return True, "map_veto_sticky_bid_wall"
    elif d == "long":
        if cascade == "long_flush" and fwd_conf > 0.5:
            return True, "map_veto_long_flush_risk"
        if stacked == "sell_stack":
            return True, "map_veto_sell_stack_imbalance"
        if sticky_side == "ask" and market.get("map_nearest_sticky_wall_pct") is not None:
            if float(market["map_nearest_sticky_wall_pct"]) <= 0.8:
                return True, "map_veto_sticky_ask_wall"

    if fwd_weight < 0.15 and cascade in {"long_flush", "short_squeeze"}:
        return False, None  # low-confidence forward — context only, no veto

    return False, None

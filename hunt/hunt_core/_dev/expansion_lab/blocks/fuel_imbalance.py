"""Block 8 — Fuel imbalance (joint price / OI / volume / funding).

Not OI alone — the *combination* is what separates "positioning" from "pump fuel":

    price flat + OI surging + negative funding   → strong pre-pump fuel
    price +3% + OI +25% + positive funding       → positioning, not a pump yet

Built from interaction terms over the already-derived market fields.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import clamp01, opt_float, safe_float
from hunt_core._dev.expansion_lab.blocks._common import abstain, result
from hunt_core._dev.expansion_lab.types import BlockContext, BlockResult

NAME = "fuel_imbalance"


def score(ctx: BlockContext) -> BlockResult:
    m = ctx.market
    oi_chg = opt_float(m.get("oi_chg_1h"))
    oi_z = opt_float(m.get("oi_z"))
    funding = opt_float(m.get("funding_pct"))
    if funding is None:
        rate = opt_float(m.get("funding_rate"))
        funding = rate * 100.0 if rate is not None else None
    chg_24h = safe_float(ctx.row.get("chg_24h_pct"))

    if oi_chg is None and oi_z is None:
        return abstain(NAME)

    oi_build = clamp01((oi_chg or 0) / 12.0) if (oi_chg or 0) > 0 else 0.0
    if oi_z is not None and oi_z > 0:
        oi_build = max(oi_build, clamp01(oi_z / 3.0))
    price_flat = clamp01(1.0 - abs(chg_24h) / 5.0)  # 1 when flat, 0 at ±5%
    evidence: list[str] = []
    direction = "neutral"

    # Pre-pump fuel: OI building, price flat, funding not crowded-long.
    up_fuel = oi_build * (0.4 + 0.6 * price_flat)
    if funding is not None and funding < 0:
        up_fuel *= 1.25
        evidence.append("oi_build+neg_funding")
    elif funding is not None and funding > 0.03:
        up_fuel *= 0.55  # positioning, not pump
        evidence.append("oi_build+crowded_long")
    up_fuel = clamp01(up_fuel)

    # Pre-dump fuel: OI building into a rally with crowded-long funding.
    down_fuel = 0.0
    if chg_24h > 3.0 and (oi_chg or 0) > 0 and funding is not None and funding > 0.02:
        down_fuel = clamp01(oi_build * clamp01(chg_24h / 12.0) * 1.2)
        evidence.append("rally+oi+crowded_long")

    if up_fuel >= down_fuel and up_fuel > 0:
        direction = "up"
        if oi_build >= 0.4 and price_flat >= 0.5 and "oi_build+neg_funding" not in evidence:
            evidence.append("oi_build_price_flat")
        return result(NAME, up_fuel, direction=direction, evidence=tuple(evidence))
    if down_fuel > 0:
        return result(NAME, down_fuel, direction="down", evidence=tuple(evidence))
    return result(NAME, up_fuel, direction="neutral", evidence=tuple(evidence))


__all__ = ["NAME", "score"]

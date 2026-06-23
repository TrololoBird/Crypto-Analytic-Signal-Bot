"""Level selection for trade plan — canonical confluence grid / structure set."""
from __future__ import annotations

from typing import Any

from hunt_core.deep.verdict_v2._helpers import atr_from_row, safe_float
from hunt_core.shared.primitives.targets import (
    collect_downward_targets as _collect_downward_targets,
    collect_upward_targets as _collect_upward_targets,
)

_STOP_BUFFER_ATR = 0.35
_CATALYST_BUFFER_ATR = 0.25


def _structure(row: dict[str, Any]) -> dict[str, Any]:
    s = row.get("structure")
    return s if isinstance(s, dict) else {}


def _key_levels(row: dict[str, Any]) -> dict[str, Any]:
    s = _structure(row)
    kl = s.get("key_levels")
    return kl if isinstance(kl, dict) else {}


def _pools(row: dict[str, Any]) -> dict[str, Any]:
    s = _structure(row)
    p = s.get("liquidity_pools")
    return p if isinstance(p, dict) else {}


def _regime(row: dict[str, Any]) -> dict[str, Any]:
    r = row.get("regime")
    return r if isinstance(r, dict) else {}


def canonical_levels(row: dict[str, Any], direction: str) -> dict[str, float]:
    """One level set for stop, catalyst, and display — confluence grid + maps."""
    kl = _key_levels(row)
    pools = _pools(row)
    regime = _regime(row)
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    out: dict[str, float] = {}
    if direction == "long":
        for key in ("support", "last_swing_low"):
            v = safe_float(kl.get(key))
            if v > 0:
                out["support"] = v
                break
        below = safe_float(pools.get("nearest_below"))
        if below > 0:
            out["pool_below"] = below
        poc = safe_float(regime.get("poc_1h") or market.get("map_vp_poc"))
        if poc > 0:
            out["poc"] = poc
        val = safe_float(market.get("map_vp_val"))
        if val > 0:
            out["val"] = val
    else:
        for key in ("resistance", "last_swing_high"):
            v = safe_float(kl.get(key))
            if v > 0:
                out["resistance"] = v
                break
        above = safe_float(pools.get("nearest_above"))
        if above > 0:
            out["pool_above"] = above
        poc = safe_float(regime.get("poc_1h") or market.get("map_vp_poc"))
        if poc > 0:
            out["poc"] = poc
        vah = safe_float(market.get("map_vp_vah"))
        if vah > 0:
            out["vah"] = vah
    return out


def pick_catalyst_level(row: dict[str, Any], direction: str) -> tuple[float, str]:
    """Sweep/reject level — same canonical set as stop, but not the stop itself."""
    levels = canonical_levels(row, direction)
    if direction == "long":
        for key in ("pool_below", "support", "val", "poc"):
            v = levels.get(key, 0)
            if v > 0:
                return v, key
    else:
        for key in ("pool_above", "resistance", "vah", "poc"):
            v = levels.get(key, 0)
            if v > 0:
                return v, key
    return 0.0, "none"


def pick_targets(row: dict[str, Any], direction: str) -> tuple[list[float], list[str]]:
    price = safe_float(row.get("price"))
    if price <= 0:
        return [], []
    if direction == "long":
        return _collect_upward_targets(row, price)
    return _collect_downward_targets(row, price)


def pick_stop(row: dict[str, Any], direction: str, entry: float, *, catalyst_level: float = 0.0) -> tuple[float, str]:
    """Stop beyond catalyst/structure by real buffer — catalyst ≠ stop by construction."""
    atr = atr_from_row(row)
    buffer = max(atr * _STOP_BUFFER_ATR, entry * 0.0008)
    cat = catalyst_level if catalyst_level > 0 else pick_catalyst_level(row, direction)[0]
    if direction == "long":
        if cat > 0 and cat < entry:
            return round(cat - buffer, 6), "beyond_catalyst_buf"
        levels = canonical_levels(row, direction)
        structural = levels.get("support") or levels.get("pool_below") or 0.0
        if structural > 0 and structural < entry:
            return round(structural - buffer, 6), "structure_below_buf"
        return round(entry - atr * 1.5, 6), "atr_fallback"
    if cat > 0 and cat > entry:
        return round(cat + buffer, 6), "beyond_catalyst_buf"
    levels = canonical_levels(row, direction)
    structural = levels.get("resistance") or levels.get("pool_above") or 0.0
    if structural > 0 and structural > entry:
        return round(structural + buffer, 6), "structure_above_buf"
    return round(entry + atr * 1.5, 6), "atr_fallback"


def stop_structural_buffer_atr(row: dict[str, Any], plan_direction: str, stop: float, entry: float) -> float:
    """Distance from stop to nearest structural level in ATR units."""
    atr = atr_from_row(row)
    if atr <= 0 or stop <= 0 or entry <= 0:
        return 1.0
    cat, _ = pick_catalyst_level(row, plan_direction)
    if cat <= 0:
        return 1.0
    if plan_direction == "long":
        gap = stop - cat
    else:
        gap = cat - stop
    if gap < 0:
        return 0.0
    return gap / atr


def entry_zone(row: dict[str, Any], direction: str, pad_atr: float) -> tuple[tuple[float, float], str]:
    """Entry zone anchored to structural reference — not raw price every tick."""
    price = safe_float(row.get("price"))
    atr = atr_from_row(row)
    pad = atr * pad_atr
    levels = canonical_levels(row, direction)
    regime = _regime(row)
    market = row.get("market") if isinstance(row.get("market"), dict) else {}

    if direction == "long":
        anchor = levels.get("poc") or levels.get("support") or safe_float(regime.get("poc_1h"))
        if anchor <= 0:
            anchor = safe_float(market.get("map_vp_poc"))
        structural = levels.get("support") or levels.get("pool_below") or 0.0
        if anchor > price * 1.002:
            # POC above price — pullback zone stays below market (limit on dip).
            hi = round(price - pad * 0.08, 6)
            lo_anchor = structural if structural > 0 and structural < price else price - pad * 1.4
            lo = round(min(lo_anchor, hi - pad * 0.35), 6)
            if lo >= hi:
                lo = round(hi - max(pad * 0.8, atr * 0.45), 6)
            return (lo, hi), "pullback_below_price"
        if anchor <= 0:
            anchor = price
        lo = round(min(anchor, price) - pad * 0.5, 6)
        hi = round(min(max(anchor, price) + pad * 0.2, price), 6)
        if lo >= hi:
            lo, hi = round(price - pad, 6), round(price - pad * 0.05, 6)
        return (lo, hi), "struct_pullback_zone"
    anchor = levels.get("poc") or levels.get("resistance") or safe_float(regime.get("poc_1h"))
    if anchor <= 0:
        anchor = safe_float(market.get("map_vp_poc"))
    if anchor <= 0:
        anchor = price
    lo = round(min(anchor, price) - pad * 0.25, 6)
    hi = round(max(anchor, price) + pad * 0.5, 6)
    if lo >= hi:
        lo, hi = round(price, 6), round(price + pad, 6)
    return (lo, hi), "struct_rally_zone"


def catalyst_buffer_atr() -> float:
    return _CATALYST_BUFFER_ATR

"""Maps-driven forecasts — predump / prepump / ignition target bands."""
from __future__ import annotations

from typing import Any, Literal

ForecastKind = Literal["predump_short", "prepump_long", "ignition_long"]


def _factor_confidence(factors: list[str], max_factors: int) -> float:
    """Confidence from structural factor count (no weighted blend)."""
    if max_factors <= 0:
        return 0.0
    return round(min(1.0, len(factors) / max_factors), 3)


def _collect_upward_targets(row: dict[str, Any], price: float) -> tuple[list[float], list[str]]:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    maps = row.get("maps") if isinstance(row.get("maps"), dict) else {}
    targets: list[float] = []
    factors: list[str] = []

    short_liq = market.get("liq_heatmap_nearest_short")
    if short_liq is not None:
        try:
            sl = float(short_liq)
            if sl > price:
                targets.append(sl)
                factors.append("short_liq_magnet")
        except (TypeError, ValueError):
            pass

    liq = maps.get("liquidation") if isinstance(maps.get("liquidation"), dict) else {}
    for z in liq.get("forward_zones") or []:
        if not isinstance(z, dict):
            continue
        pc = z.get("price_center")
        if pc is None:
            continue
        try:
            fp = float(pc)
            if fp > price:
                targets.append(fp)
                if "forward_zone" not in factors:
                    factors.append("forward_zone")
        except (TypeError, ValueError):
            continue

    vp = maps.get("volume_profile") if isinstance(maps.get("volume_profile"), dict) else {}
    for prof in vp.get("profiles") or []:
        if not isinstance(prof, dict):
            continue
        for node in prof.get("hvn_nodes") or []:
            if not isinstance(node, dict):
                continue
            p = node.get("price")
            if p is None:
                continue
            try:
                fp = float(p)
                if fp > price:
                    targets.append(fp)
            except (TypeError, ValueError):
                continue
        naked = prof.get("naked_poc")
        if naked is not None:
            try:
                np = float(naked)
                if np > price:
                    targets.append(np)
                    if "naked_poc" not in factors:
                        factors.append("naked_poc")
            except (TypeError, ValueError):
                pass

    void_above = market.get("map_void_above")
    if void_above is not None:
        try:
            vp = float(void_above)
            if vp > price:
                targets.append(vp)
                if "void_path" not in factors:
                    factors.append("void_path")
        except (TypeError, ValueError):
            pass

    return targets, factors


def _collect_downward_targets(row: dict[str, Any], price: float) -> tuple[list[float], list[str]]:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    maps = row.get("maps") if isinstance(row.get("maps"), dict) else {}
    session = row.get("session") if isinstance(row.get("session"), dict) else {}
    targets: list[float] = []
    factors: list[str] = []

    long_liq = market.get("liq_heatmap_nearest_long")
    if long_liq is not None:
        try:
            ll = float(long_liq)
            if ll < price:
                targets.append(ll)
                factors.append("long_liq_magnet")
        except (TypeError, ValueError):
            pass

    liq = maps.get("liquidation") if isinstance(maps.get("liquidation"), dict) else {}
    for z in liq.get("forward_zones") or []:
        if not isinstance(z, dict):
            continue
        pc = z.get("price_center")
        if pc is None:
            continue
        try:
            fp = float(pc)
            if fp < price:
                targets.append(fp)
                if "forward_liq_zone" not in factors:
                    factors.append("forward_liq_zone")
        except (TypeError, ValueError):
            continue

    vp = maps.get("volume_profile") if isinstance(maps.get("volume_profile"), dict) else {}
    for prof in vp.get("profiles") or []:
        if not isinstance(prof, dict):
            continue
        val = prof.get("val")
        if val is not None:
            try:
                v = float(val)
                if v < price:
                    targets.append(v)
                    if "val_magnet" not in factors:
                        factors.append("val_magnet")
            except (TypeError, ValueError):
                pass

    hunt_low = session.get("hunt_low") or session.get("low_24h")
    if hunt_low is not None:
        try:
            hl = float(hunt_low)
            if hl < price:
                targets.append(hl)
                if "range_low" not in factors:
                    factors.append("range_low")
        except (TypeError, ValueError):
            pass

    void_below = market.get("map_void_below")
    if void_below is not None:
        try:
            vb = float(void_below)
            if vb < price:
                targets.append(vb)
                if "void_path_down" not in factors:
                    factors.append("void_path_down")
        except (TypeError, ValueError):
            pass

    from hunt_core.maps.oi import oi_regime_from_row

    if oi_regime_from_row(row) == "new_money_short":
        factors.append("oi_new_money_short")
    cvd = str(market.get("map_cvd_divergence") or "")
    if cvd == "bearish_div":
        factors.append("bear_cvd_div")

    return targets, factors


def build_maps_forecast(row: dict[str, Any]) -> dict[str, Any] | None:
    """Pre-pump (coil) forecast — upward structural targets."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return None
    market = row.get("market") if isinstance(row.get("market"), dict) else {}

    targets, factors = _collect_upward_targets(row, price)
    if market.get("map_accum_bid_absorption"):
        factors.append("bid_absorption")
    if market.get("map_ask_thinning"):
        factors.append("ask_thinning")
    if market.get("map_cvd_divergence") == "bullish_div":
        factors.append("bull_cvd_div")
    contraction = market.get("map_vp_va_contraction")
    if contraction is not None and float(contraction) < 0.85:
        factors.append("va_contraction")
    acc = float(market.get("map_vp_accumulation") or 0)
    if acc >= 0.55:
        factors.append("vp_accumulation")

    factors = list(dict.fromkeys(factors))
    if not targets:
        return None

    target_lo = min(targets)
    target_hi = max(targets)
    expected_move_pct = (target_lo - price) / price * 100.0
    confidence = _factor_confidence(factors, max_factors=6)

    return {
        "kind": "prepump_long",
        "direction": "long",
        "target_lo": round(target_lo, 6),
        "target_hi": round(target_hi, 6),
        "target_primary": round(target_lo, 6),
        "expected_move_pct": round(expected_move_pct, 2),
        "confidence": confidence,
        "factors": factors,
    }


def build_dump_forecast(row: dict[str, Any]) -> dict[str, Any] | None:
    """Pre-dump short forecast — downward markdown zone."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return None

    targets, factors = _collect_downward_targets(row, price)
    factors = list(dict.fromkeys(factors))
    if not targets:
        return None

    target_lo = min(targets)
    target_hi = max(targets)
    expected_move_pct = (target_lo - price) / price * 100.0
    confidence = _factor_confidence(factors, max_factors=5)

    return {
        "kind": "predump_short",
        "direction": "short",
        "target_lo": round(target_lo, 6),
        "target_hi": round(target_hi, 6),
        "target_primary": round(target_lo, 6),
        "expected_move_pct": round(expected_move_pct, 2),
        "confidence": confidence,
        "factors": factors,
    }


def build_ignition_forecast(row: dict[str, Any]) -> dict[str, Any] | None:
    """Squeeze ignition forecast — short-liq magnet above + time window."""
    price = float(row.get("price") or 0)
    if price <= 0:
        return None
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    r1h = tf.get("1h") or {}

    targets, factors = _collect_upward_targets(row, price)
    atr = float(r1h.get("atr14") or r1h.get("atr") or 0)
    if atr > 0:
        atr_target = price + atr * 1.2
        targets.append(atr_target)
        factors.append("atr_1h_magnet")

    funding = market.get("funding_rate") or market.get("live_funding_rate")
    if funding is not None and float(funding) < -0.0001:
        factors.append("neg_funding")
    if market.get("map_cvd_divergence") == "bullish_div":
        factors.append("cvd_absorption")

    factors = list(dict.fromkeys(factors))
    if not targets:
        return None

    target_lo = min(targets)
    target_hi = max(targets)
    expected_move_pct = (target_lo - price) / price * 100.0
    confidence = _factor_confidence(factors, max_factors=5)

    return {
        "kind": "ignition_long",
        "direction": "long",
        "target_lo": round(target_lo, 6),
        "target_hi": round(target_hi, 6),
        "target_primary": round(target_lo, 6),
        "expected_move_pct": round(expected_move_pct, 2),
        "confidence": confidence,
        "factors": factors,
        "window_minutes": 15,
    }


def build_all_forecasts(row: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Build all three forecast kinds; attach best match to row caller."""
    return {
        "prepump_long": build_maps_forecast(row),
        "predump_short": build_dump_forecast(row),
        "ignition_long": build_ignition_forecast(row),
    }


def stamp_forecasts_on_row(row: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Evaluate forecasts and stamp primary + all on row."""
    all_fc = build_all_forecasts(row)
    row["forecasts"] = {k: v for k, v in all_fc.items() if v is not None}
    fusion = row.get("manipulation_fusion") if isinstance(row.get("manipulation_fusion"), dict) else {}
    archetype = str(fusion.get("archetype") or "")
    primary_key = {
        "predump_short": "predump_short",
        "coil_long": "prepump_long",
        "ignition_long": "ignition_long",
    }.get(archetype, "prepump_long")
    primary = all_fc.get(primary_key) or build_maps_forecast(row)
    if primary:
        row["maps_forecast"] = primary
    return all_fc


__all__ = [
    "ForecastKind",
    "build_all_forecasts",
    "build_dump_forecast",
    "build_ignition_forecast",
    "build_maps_forecast",
    "stamp_forecasts_on_row",
]

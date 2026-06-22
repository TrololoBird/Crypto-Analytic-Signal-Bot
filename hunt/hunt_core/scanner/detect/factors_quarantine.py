"""Lake-backed fusion factors — promoted to production via factor_registry.json.

``market_maker_trap`` and ``whale_activity`` remain quarantine-only (live row context).
Production factors are wired through ``detect/factors.compute_factors``.
"""
from __future__ import annotations

from typing import Any

from hunt_core.scanner.detect import calibrate as C
from hunt_core.scanner.detect.factors import (
    AMPLIFIER,
    DIRECTIONAL,
    FactorScore,
    _abstain,
    _mean_active,
)
from hunt_core.scanner.detect.windows import FeatureWindow


def factor_oi_acceleration(window: FeatureWindow) -> FactorScore:
    """OI slope minus change — second derivative of positioning."""
    z = C.robust_z(window.col("oi_acceleration"))
    if z is None:
        z_chg = C.robust_z(window.col("oi_change_pct"))
        z_slope = C.robust_z(window.col("oi_slope_5m"))
        if z_chg is None and z_slope is None:
            return _abstain("oi_acceleration", DIRECTIONAL, "oi_accel_missing")
        parts = {k: v for k, v in (("oi_change_pct", z_chg), ("oi_slope_5m", z_slope)) if v is not None}
        z = sum(parts.values()) / len(parts)
        return FactorScore(
            "oi_acceleration",
            DIRECTIONAL,
            z,
            True,
            f"oi_accel_proxy={z:+.2f}",
            parts,
        )
    return FactorScore("oi_acceleration", DIRECTIONAL, z, True, f"oi_accel_z={z:+.2f}", {"oi_acceleration": z})


def factor_funding_velocity(window: FeatureWindow) -> FactorScore:
    """Funding trend velocity — rising crowded longs ⇒ short pressure (contrarian)."""
    z = C.robust_z(window.col("funding_velocity"))
    if z is None:
        z = C.ols_slope(window.col("funding_velocity"))
    if z is None:
        return _abstain("funding_velocity", DIRECTIONAL, "funding_velocity_missing")
    score = -z
    return FactorScore(
        "funding_velocity",
        DIRECTIONAL,
        score,
        True,
        f"funding_vel_z={score:+.2f}",
        {"funding_velocity": z},
    )


def factor_poc_migration(window: FeatureWindow) -> FactorScore:
    """POC migration on 1h/4h VP — up ⇒ long, down ⇒ short."""
    z1 = C.robust_z(window.col("poc_migration_1h"))
    z4 = C.robust_z(window.col("poc_migration_4h"))
    score, parts = _mean_active({"poc_migration_1h": z1, "poc_migration_4h": z4})
    if score is None:
        return _abstain("poc_migration", DIRECTIONAL, "poc_migration_missing")
    return FactorScore("poc_migration", DIRECTIONAL, score, True, f"poc_mig_z={score:+.2f}", parts)


def factor_va_contraction(window: FeatureWindow) -> FactorScore:
    """Value-area contraction — coil amplifier behind whichever side wins."""
    z = C.robust_z(window.col("va_contraction"))
    if z is None:
        last = window.last("va_contraction")
        if last is None:
            return _abstain("va_contraction", AMPLIFIER, "va_contraction_missing")
        z = last
    coil = max(0.0, z)
    return FactorScore("va_contraction", AMPLIFIER, coil, True, f"va_coil={coil:.2f}", {"va_contraction": z})


def factor_liquidity_void_path(window: FeatureWindow) -> FactorScore:
    """Liquidity void overhead — thin air above ⇒ long vacuum impulse."""
    z = C.robust_z(window.col("liquidity_void_path"))
    if z is None:
        last = window.last("liquidity_void_path")
        if last is None:
            return _abstain("liquidity_void_path", DIRECTIONAL, "void_path_missing")
        score = max(-1.0, min(1.0, (5.0 - float(last)) / 5.0))
        return FactorScore(
            "liquidity_void_path",
            DIRECTIONAL,
            score,
            True,
            f"void_dist={last:.2f}",
            {"liquidity_void_path": last},
        )
    score = -z
    return FactorScore(
        "liquidity_void_path",
        DIRECTIONAL,
        score,
        True,
        f"void_z={score:+.2f}",
        {"liquidity_void_path": z},
    )


def factor_market_maker_trap(window: FeatureWindow, *, row: dict[str, Any] | None = None) -> FactorScore:
    """CHoCH / sweep reclaim at mapped level — bear trap long, bull trap short."""
    if not row:
        return _abstain("market_maker_trap", AMPLIFIER, "row_context_missing")
    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    event = str(structure.get("event") or structure.get("bos_choch") or "").lower()
    choch = bool(structure.get("choch_detected")) or "choch" in event
    at_level = bool(structure.get("at_level"))
    bos = str(structure.get("bos_direction") or "")
    bsl_sweep = bool(structure.get("bsl_sweep"))
    support_break = bool(structure.get("support_break"))
    cvd = str(market.get("map_cvd_divergence") or "")

    has_trap = (choch and at_level) or bsl_sweep or support_break
    if not has_trap:
        return _abstain("market_maker_trap", AMPLIFIER, "no_trap_signature")

    strength = 0.5
    if choch and at_level:
        strength += 0.2
    if bsl_sweep:
        strength += 0.1
    if cvd in {"bullish_div", "bearish_div"}:
        strength += 0.15
    # Registry kind=amplifier — unsigned magnitude; direction stored in detail only.
    return FactorScore(
        "market_maker_trap",
        AMPLIFIER,
        min(1.0, strength),
        True,
        f"trap bos={bos} cvd={cvd}",
        {"strength": strength},
    )


def factor_whale_activity(window: FeatureWindow, *, row: dict[str, Any] | None = None) -> FactorScore:
    """Iceberg / sticky walls — conviction amplifier from book maps."""
    del window
    if not row:
        return _abstain("whale_activity", AMPLIFIER, "row_context_missing")
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    try:
        ice_n = int(market.get("map_iceberg_count") or 0)
    except (TypeError, ValueError):
        ice_n = 0
    try:
        sticky_n = int(market.get("map_sticky_wall_count") or 0)
    except (TypeError, ValueError):
        sticky_n = 0
    bid_wall = market.get("nearest_bid_wall")
    ask_wall = market.get("nearest_ask_wall")
    if not ice_n and not sticky_n and bid_wall is None and ask_wall is None:
        return _abstain("whale_activity", AMPLIFIER, "whale_maps_missing")

    parts: list[float] = []
    if ice_n:
        parts.append(min(1.0, ice_n / 4.0))
    if sticky_n:
        parts.append(min(1.0, sticky_n / 4.0))
    if not parts:
        parts.append(0.4)
    score = sum(parts) / len(parts)
    return FactorScore(
        "whale_activity",
        AMPLIFIER,
        score,
        True,
        f"whale ice={ice_n} sticky={sticky_n}",
        {"iceberg": float(ice_n), "sticky": float(sticky_n)},
    )


def _row_float(row: dict[str, Any] | None, *keys: str) -> float | None:
    if not row:
        return None
    for key in keys:
        val = row.get(key)
        if val is None and isinstance(row.get("market"), dict):
            val = row["market"].get(key)
        if val is None and isinstance(row.get("cross_exchange"), dict):
            val = row["cross_exchange"].get(key.replace("cross_", ""))
        if val is None:
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if f == f:
            return f
    return None


def factor_cross_exchange_divergence(
    window: FeatureWindow, *, row: dict[str, Any] | None = None
) -> FactorScore:
    """Multi-venue price/funding divergence — positive ⇒ local venue rich (short lean)."""
    price_div = _row_float(row, "cross_price_divergence_pct")
    fund_spread = _row_float(row, "cross_funding_spread")
    if price_div is None and fund_spread is None:
        return _abstain("cross_exchange_divergence", DIRECTIONAL, "cross_venue_missing")
    parts: dict[str, float] = {}
    score = 0.0
    n = 0
    if price_div is not None:
        z = C.robust_z(window.col("cross_price_divergence_pct")) if window.has("cross_price_divergence_pct") else None
        contrib = z if z is not None else price_div
        parts["cross_price_divergence_pct"] = contrib
        score += contrib
        n += 1
    if fund_spread is not None:
        z = C.robust_z(window.col("cross_funding_spread")) if window.has("cross_funding_spread") else None
        contrib = -(z if z is not None else fund_spread)
        parts["cross_funding_spread"] = contrib
        score += contrib
        n += 1
    score /= max(n, 1)
    return FactorScore(
        "cross_exchange_divergence",
        DIRECTIONAL,
        score,
        True,
        f"cross_div={score:+.2f}",
        parts,
    )


def factor_cross_funding_consensus(
    window: FeatureWindow, *, row: dict[str, Any] | None = None
) -> FactorScore:
    """Cross-venue funding consensus — crowded longs ⇒ short pressure."""
    raw = row.get("cross_funding_consensus") if row else None
    if raw is None and row and isinstance(row.get("cross_exchange"), dict):
        raw = row["cross_exchange"].get("funding_consensus")
    if raw is None:
        return _abstain("cross_funding_consensus", DIRECTIONAL, "cross_funding_missing")
    text = str(raw).lower()
    if text in {"long", "bull", "crowded_long"}:
        score = -0.75
    elif text in {"short", "bear", "crowded_short"}:
        score = 0.75
    elif text in {"neutral", "balanced"}:
        score = 0.0
    else:
        try:
            score = -float(raw)
        except (TypeError, ValueError):
            return _abstain("cross_funding_consensus", DIRECTIONAL, "cross_funding_unparsed")
    return FactorScore(
        "cross_funding_consensus",
        DIRECTIONAL,
        score,
        True,
        f"cross_fund={text or raw}",
        {"cross_funding_consensus": score},
    )


def factor_spot_futures_pressure(
    window: FeatureWindow, *, row: dict[str, Any] | None = None
) -> FactorScore:
    """Spot lead + basis slope — spot leading up ⇒ long pressure."""
    spot_lead = _row_float(row, "spot_lead_return_1m")
    basis = _row_float(row, "spot_futures_spread_bps", "basis_bps")
    prem_slope = _row_float(row, "premium_slope_5m")
    if spot_lead is None and basis is None and prem_slope is None:
        return _abstain("spot_futures_pressure", DIRECTIONAL, "spot_basis_missing")
    parts: dict[str, float] = {}
    vals: list[float] = []
    if spot_lead is not None:
        z = C.robust_z(window.col("spot_lead_return_1m")) if window.has("spot_lead_return_1m") else spot_lead * 100.0
        parts["spot_lead_return_1m"] = z
        vals.append(z)
    if basis is not None:
        parts["spot_futures_spread_bps"] = -basis / 10.0
        vals.append(-basis / 10.0)
    if prem_slope is not None:
        z = C.robust_z(window.col("premium_slope_5m")) if window.has("premium_slope_5m") else prem_slope
        parts["premium_slope_5m"] = z
        vals.append(z)
    score = sum(vals) / len(vals)
    return FactorScore(
        "spot_futures_pressure",
        DIRECTIONAL,
        score,
        True,
        f"spot_fut={score:+.2f}",
        parts,
    )


_ROW_CONTEXT_QUARANTINE = frozenset(
    {
        "market_maker_trap",
        "whale_activity",
        "cross_exchange_divergence",
        "cross_funding_consensus",
        "spot_futures_pressure",
    }
)

_QUARANTINE_FACTORS = (
    factor_market_maker_trap,
    factor_whale_activity,
    factor_cross_exchange_divergence,
    factor_cross_funding_consensus,
    factor_spot_futures_pressure,
)


def compute_quarantine_factors(
    window: FeatureWindow,
    *,
    row: dict[str, Any] | None = None,
) -> list[FactorScore]:
    """Shadow factor readings (registry status=quarantine only)."""
    scores: list[FactorScore] = []
    for fn in _QUARANTINE_FACTORS:
        name = fn.__name__.replace("factor_", "")
        if name in _ROW_CONTEXT_QUARANTINE:
            scores.append(fn(window, row=row))
        else:
            scores.append(fn(window))
    try:
        from hunt_core.scanner.detect.factor_registry_loader import quarantine_factors

        allowed = quarantine_factors()
        return [s for s in scores if s.name in allowed]
    except Exception:
        return scores


__all__ = [
    "compute_quarantine_factors",
    "factor_cross_exchange_divergence",
    "factor_cross_funding_consensus",
    "factor_funding_velocity",
    "factor_liquidity_void_path",
    "factor_market_maker_trap",
    "factor_oi_acceleration",
    "factor_poc_migration",
    "factor_spot_futures_pressure",
    "factor_va_contraction",
    "factor_whale_activity",
]

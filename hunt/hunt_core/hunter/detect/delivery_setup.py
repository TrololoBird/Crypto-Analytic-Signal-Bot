"""Bridge a Detection into the full delivery setup contract.

The fusion engine decides *whether* and *which side*; the trade geometry (entry zone,
SL, TP ladder, invalidation) is still produced by the preserved ``levels.py`` so the
delivery path (contract → dispatch → telegram), tracker, and templates keep reading the
same setup dict shape. This is the keystone that lets the old scan/scoring stack be
removed without breaking downstream consumers.

The contextual inputs the geometry needs (impulse extremes, fib ladder, ATR, local
pivots, range/leg/fall %) are read from the already-assembled snapshot ``row`` — these
come from the preserved feature/structure/levels layer, not from the old detection
stack.
"""
from __future__ import annotations

from typing import Any

from hunt_core.hunter.detect.result import Detection

# Map the fusion phase onto the legacy lifecycle-phase vocabulary that levels.py uses to
# pick adaptive SL/TP caps and min-RR. Presentation/compat mapping for geometry only —
# it does not gate anything.
_PHASE_TO_LEVELS = {
    "pre_pump": "accumulation",
    "pre_dump": "distribution",
    "mid": "",
    "neutral": "",
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f == f else default  # drop NaN


def _tf_atr(row: dict[str, Any], tf_key: str) -> float | None:
    tf = row.get("timeframes")
    if not isinstance(tf, dict):
        return None
    block = tf.get(tf_key) or tf.get(f"{tf_key}_closed")
    if not isinstance(block, dict):
        return None
    atr = block.get("atr14")
    return _f(atr) if atr is not None else None


def _local_levels(row: dict[str, Any]) -> tuple[float, float]:
    """Local support/resistance from the snapshot structure, 0 when absent (the geometry
    function falls back to the impulse extremes)."""
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    sup = _f(lc.get("local_support"))
    res = _f(lc.get("local_resistance"))
    struct = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    if sup <= 0:
        sup = _f(struct.get("local_support") or struct.get("support"))
    if res <= 0:
        res = _f(struct.get("local_resistance") or struct.get("resistance"))
    return sup, res


def _geometry(detection: Detection, row: dict[str, Any]) -> dict[str, Any]:
    from hunt_core.levels.levels import structural_long_levels, structural_short_levels

    price = _f(row.get("price")) or _f(detection.price)
    impulse_high = _f(row.get("impulse_high"))
    impulse_low = _f(row.get("impulse_low"))
    fib = row.get("fib") if isinstance(row.get("fib"), dict) else {}
    fib_hunt = fib.get("hunt") if isinstance(fib.get("hunt"), dict) else fib
    atr15 = _tf_atr(row, "15m") or 0.0
    atr1h = _tf_atr(row, "1h")
    sup, res = _local_levels(row)
    session = row.get("session") if isinstance(row.get("session"), dict) else {}
    range_pct_24h = _f(session.get("range_pct_24h"))
    leg_gain_pct = round((impulse_high - impulse_low) / impulse_low * 100.0, 2) if impulse_low > 0 else 0.0
    fall_from_high_pct = (
        round((impulse_high - price) / impulse_high * 100.0, 2) if impulse_high > 0 else 0.0
    )
    lc_phase = _PHASE_TO_LEVELS.get(detection.phase, "")
    common = dict(
        price=price,
        impulse_high=impulse_high,
        impulse_low=impulse_low,
        fib=fib_hunt,  # type: ignore[arg-type]
        atr15=atr15,
        atr1h=atr1h,
        local_support=sup,
        local_resistance=res,
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        fall_from_high_pct=fall_from_high_pct,
        symbol=detection.symbol,
        lifecycle_phase=lc_phase,
    )
    if detection.side == "short":
        return dict(structural_short_levels(**common))
    return dict(structural_long_levels(**common))


def _check_intra_bar(detection: Detection, intra_bar: Any) -> bool:
    if intra_bar is None:
        return False
    try:
        sig = intra_bar.compute(detection.symbol)
        if sig is None:
            return False
        if sig.side != detection.side:
            return False
        return sig.confidence >= 0.3
    except Exception:
        _LOG.warning("intra_bar_check_failed | symbol=%s", detection.symbol, exc_info=True)
        return False


def build_delivery_setup(detection: Detection, row: dict[str, Any], *, intra_bar: Any = None) -> dict[str, Any]:
    """Full setup contract for the delivery path from a fusion Detection + snapshot row.

    Geometry comes from levels.py; the decision (side, confirmed, confidence_score, phase) and the
    factor evidence come from the Detection. Flag fields the old advisory/EV stack set
    are defaulted off — the fusion gate is the single delivery decision.

    Dual-gate: pre_phase signals get wider entry, tighter SL, forecast label.
    """
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    is_pre = detection.signal_type == "pre_phase"
    setup: dict[str, Any] = {
        "symbol": detection.symbol,
        "direction": detection.side,
        "impulse_confirmed": detection.gate_open or detection.pre_gate_open,
        "signal_type": detection.signal_type,
        "fusion_score": round(detection.fusion.fusion_score, 1),
        "fusion_strength": round(detection.fusion.fusion_score / 100.0, 4),
        "confidence_score": round(detection.fusion.fusion_score / 100.0, 4),
        "magnitude": round(detection.magnitude, 4),
        "vol_adj_magnitude": round(detection.gate.vol_adjusted_magnitude, 4),
        "phase": detection.phase,
        "lifecycle_phase": detection.phase,
        "forecast": is_pre,
        "confirm_hard": [f.name for f in detection.active_factors if f.kind == "directional"],
        "triggers": {f.name: round(f.score, 3) for f in detection.active_factors},
        "quarantine_factors": {
            f.name: round(f.score, 4) for f in detection.quarantine_factors if f.active
        },
        "liquidation_score": market.get("liquidation_score_5m") or market.get("liquidation_score"),
        "advisory": False,
        "anticipation": False,
        "early_tier": None,
        "intrabar_confirmed": _check_intra_bar(detection, intra_bar),
        "ev_primary": None,
        "gate_reason": detection.gate.reason,
        "pre_gate_open": detection.pre_gate_open,
    }
    if detection.pre_gate is not None:
        setup["pre_gate"] = {
            "open": detection.pre_gate.pre_gate_open,
            "energy_hits": detection.pre_gate.energy_hits,
            "structure_score": round(detection.pre_gate.structure_score, 3),
            "reason": detection.pre_gate.reason,
        }
    side_score = round(detection.fusion.fusion_score, 1)
    setup["dump_score" if detection.side == "short" else "long_score"] = side_score

    try:
        geom = _geometry(detection, row)
        setup.update(geom)
    except Exception as exc:  # geometry must never crash the tick
        setup["levels_viable"] = False
        setup["levels_veto"] = [f"geometry_error:{type(exc).__name__}"]

    _apply_pre_phase_sl_guard(setup, side=detection.side)

    from hunt_core.contract import compute_setup_risk_reward

    rr = compute_setup_risk_reward(setup, direction=detection.side or "long")
    if rr is not None:
        setup["risk_reward"] = round(float(rr), 4)
    return setup


def _apply_pre_phase_sl_guard(setup: dict[str, Any], side: str) -> None:
    """Pre-phase SL guard: push SL outside entry zone + assert invariant.

    For pre_phase signals (low magnitude, high uncertainty) the geometry may place
    SL inside the entry zone — late price confirmation can invalidate before fill.
    This guard pushes SL further out to guarantee SL < entry_lo (long) / SL > entry_hi
    (short), then asserts the invariant for 100% of pre_phase setups.
    """
    if setup.get("signal_type") != "pre_phase":
        return

    entry = setup.get("entry_zone")
    sl = setup.get("stop_loss")
    if not isinstance(entry, (list, tuple)) or len(entry) < 2 or sl is None:
        return

    entry_lo, entry_hi = float(entry[0]), float(entry[1])
    if side == "long":
        safe_sl = min(float(sl), entry_lo * 0.998)
        setup["stop_loss"] = round(safe_sl, 6)
        assert safe_sl < entry_lo, (
            f"Pre-phase long SL {safe_sl:.6g} >= entry_lo {entry_lo:.6g}"
        )
    elif side == "short":
        safe_sl = max(float(sl), entry_hi * 1.002)
        setup["stop_loss"] = round(safe_sl, 6)
        assert safe_sl > entry_hi, (
            f"Pre-phase short SL {safe_sl:.6g} <= entry_hi {entry_hi:.6g}"
        )


__all__ = ["build_delivery_setup"]

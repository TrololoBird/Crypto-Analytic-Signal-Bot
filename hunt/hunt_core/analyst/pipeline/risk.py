from __future__ import annotations

from typing import Any

from hunt_core.analyst.pipeline._helpers import safe_float
from hunt_core.analyst.pipeline.config import RiskConfig
from hunt_core.analyst.pipeline.types import MarketRegime, ModuleResult, RiskLevels
from hunt_core.analyst.pipeline.regime import RegimeParameters


def _resolve_atr(row: dict[str, Any], tf_key: str = "4h") -> tuple[float, float]:
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    for key in (f"{tf_key}_closed", tf_key, "1h_closed", "1h"):
        snap = tf.get(key)
        if isinstance(snap, dict) and snap.get("status") != "empty":
            atr = safe_float(snap.get("atr14"))
            atr_pct = safe_float(snap.get("atr_pct"))
            if atr > 0:
                return atr, atr_pct
    price = safe_float(row.get("price"))
    if price > 0:
        return price * 0.02, 2.0
    return 0.0, 0.0


def run_risk_module(
    row: dict[str, Any],
    cfg: RiskConfig,
    direction: str = "long",
    *,
    sizing_modifier: float = 1.0,
    regime: MarketRegime = MarketRegime.NORMAL,
    regime_params: RegimeParameters | None = None,
    ker: float | None = None,
) -> tuple[ModuleResult, RiskLevels | None]:
    price = safe_float(row.get("price"))
    if price <= 0:
        return ModuleResult(status="UNKNOWN", reason="Нет цены для расчёта уровней"), None

    atr_value, atr_pct = _resolve_atr(row)
    if atr_value <= 0:
        atr_pct = 2.0
        atr_value = price * atr_pct / 100.0

    rp = regime_params
    sl_mult = rp.atr_multiplier_sl if rp and rp.atr_multiplier_sl != 1.0 else cfg.atr_multiplier_sl_base
    risk_raw = atr_value * sl_mult

    sl_min = price * cfg.sl_min_pct / 100.0
    sl_max = price * cfg.sl_max_pct / 100.0
    risk_distance = max(sl_min, min(risk_raw, sl_max))

    if direction == "long":
        stop_loss = price - risk_distance
        entry_lo = price * 0.998
        entry_hi = price * 1.002
    else:
        stop_loss = price + risk_distance
        entry_lo = price * 0.998
        entry_hi = price * 1.002

    risk_amount = abs(price - stop_loss)

    is_strong_trend = ker is not None and ker > 0.60
    if is_strong_trend:
        tp1_mul = 1.5
        tp2_mul = 2.5
        tp3_mul = 4.0
    else:
        tp1_mul = cfg.r_multiplier_tp1
        tp2_mul = cfg.r_multiplier_tp2
        tp3_mul = cfg.r_multiplier_tp3

    if direction == "long":
        tp1 = price + risk_amount * tp1_mul
        tp2 = price + risk_amount * tp2_mul if tp2_mul > 0 else None
        tp3 = price + risk_amount * tp3_mul if tp3_mul > 0 else None
    else:
        tp1 = price - risk_amount * tp1_mul
        tp2 = price - risk_amount * tp2_mul if tp2_mul > 0 else None
        tp3 = price - risk_amount * tp3_mul if tp3_mul > 0 else None

    reward_tp1 = abs(tp1 - price) if tp1 > 0 else 0
    rr_tp1 = reward_tp1 / risk_amount if risk_amount > 0 else 0.0
    rr_tp2 = (abs(tp2 - price) / risk_amount) if tp2 is not None and risk_amount > 0 else None
    rr_tp3 = (abs(tp3 - price) / risk_amount) if tp3 is not None and risk_amount > 0 else None

    if atr_pct > 5.0:
        ttl = cfg.ttl_high_vol_hours
    elif atr_pct < 2.0:
        ttl = cfg.ttl_low_vol_hours
    elif rp and rp.ttl_hours > 0:
        ttl = rp.ttl_hours
    else:
        ttl = cfg.ttl_hours

    if rp:
        sizing_mod = rp.sizing_pct / 100.0
    else:
        sizing_mod = sizing_modifier

    levels = RiskLevels(
        entry_lo=round(entry_lo, 6),
        entry_hi=round(entry_hi, 6),
        stop_loss=round(stop_loss, 6),
        tp1=round(tp1, 6),
        tp2=round(tp2, 6) if tp2 else None,
        tp3=round(tp3, 6) if tp3 else None,
        rr_tp1=round(rr_tp1, 2),
        rr_tp2=round(rr_tp2, 2) if rr_tp2 is not None else None,
        rr_tp3=round(rr_tp3, 2) if rr_tp3 is not None else None,
        atr_pct=round(atr_pct, 2),
        sizing_modifier=sizing_mod,
        ttl_hours=ttl,
    )

    evidence = [
        f"atr={atr_pct:.2f}%",
        f"sl={levels.stop_loss}",
        f"tp1={levels.tp1} (R:{rr_tp1:.1f})",
        f"ttl={ttl:.0f}h",
    ]

    return ModuleResult(
        status="PASS",
        reason=f"SL={levels.stop_loss}, TP1={levels.tp1} ({rr_tp1:.1f}R), ATR={atr_pct:.2f}%, TTL={ttl:.0f}ч",
        details={"atr_pct": atr_pct, "regime": regime.value if hasattr(regime, "value") else str(regime), "evidence": evidence},
    ), levels

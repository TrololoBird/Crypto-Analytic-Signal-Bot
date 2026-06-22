"""Wash-trading and kinematic chase gates (A8–A10)."""
from __future__ import annotations

import logging
import math
from typing import Any

LOG = logging.getLogger(__name__)

_WASH_CALIBRATION_DEFAULTS: dict[str, float] = {
    "wash_z_threshold": 4.0,
    "wti_threshold": 0.65,
    "max_velocity_z": 4.5,
}
_WASH_CALIBRATION_FAIL_CLOSED: dict[str, float] = {
    "wash_z_threshold": 3.0,
    "wti_threshold": 0.50,
    "max_velocity_z": 3.5,
}

_KINEMATIC_EXEMPT_SHORT_PHASES = frozenset({"exhaustion_at_high", "distribution"})
# dump_active intentionally excluded — mid-dump chase still subject to kinematic gate.
_KINEMATIC_EXEMPT_LONG_PHASES = frozenset(
    {"post_dump_bounce", "recovery", "accumulation"}
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _has_real_quote_vol_baseline(*, market: dict[str, Any], row: dict[str, Any]) -> bool:
    for source in (market, row):
        raw = source.get("quote_vol_baseline")
        if raw is None:
            continue
        baseline = _optional_float(raw)
        if baseline is not None and baseline > 0.0:
            return True
    return False


def _quote_volume_fields_present(
    *, market: dict[str, Any], row: dict[str, Any], tf15: dict[str, Any]
) -> bool:
    for source in (row, market, tf15):
        for key in ("quote_volume", "quote_volume_24h"):
            if source.get(key) is not None:
                return True
    return False


def wash_volume_z_score(
    *,
    quote_volume: float,
    baseline_volume: float,
    sigma: float = 0.0,
) -> float:
    if baseline_volume <= 0:
        return 0.0
    if sigma > 0:
        return (quote_volume - baseline_volume) / sigma
    ratio = quote_volume / baseline_volume
    if ratio <= 1.0:
        return 0.0
    return min(6.0, math.log(ratio) * 2.0)


def wash_trading_index(
    *,
    price_change_pct: float,
    volume_z: float,
) -> float:
    if abs(price_change_pct) >= 3.0:
        return 0.0
    if volume_z < 2.0:
        return 0.0
    return round(min(1.0, volume_z / 6.0), 3)


def pump_dump_stage(
    *,
    change_24h_pct: float,
    pos_in_range: float | None,
    volume_z: float,
) -> str | None:
    pos = pos_in_range if pos_in_range is not None else 0.5
    if change_24h_pct >= 15.0 and volume_z >= 2.0 and pos >= 0.75:
        return "pump_peak"
    if change_24h_pct >= 8.0 and volume_z >= 1.5 and pos >= 0.65:
        return "pump_active"
    if change_24h_pct <= -8.0 and pos <= 0.35 and volume_z >= 1.0:
        return "dump_active"
    if change_24h_pct <= -15.0 and pos <= 0.25:
        return "dump_exhaustion"
    return None


def kinematic_z(
    *,
    change_1h_pct: float,
    change_24h_pct: float,
) -> tuple[float, float]:
    velocity = change_1h_pct
    expected_1h = change_24h_pct / 24.0
    acceleration = velocity - expected_1h
    v_z = max(-6.0, min(6.0, velocity / 3.0))
    a_z = max(-6.0, min(6.0, acceleration / 2.0))
    return round(v_z, 2), round(a_z, 2)


def _wash_calibrated_thresholds() -> dict[str, float]:
    defaults = _WASH_CALIBRATION_DEFAULTS
    try:
        from hunt_core.params.store import load_calibration

        cal = load_calibration()
        wk = (cal.get("outcome_calibration") or {}).get("wash_kinematic") or {}
        if not isinstance(wk, dict):
            LOG.warning(
                "wash calibration invalid type=%s; using fail-closed thresholds",
                type(wk).__name__,
            )
            return dict(_WASH_CALIBRATION_FAIL_CLOSED)
        return {
            "wash_z_threshold": float(wk.get("wash_z_threshold", defaults["wash_z_threshold"])),
            "wti_threshold": float(wk.get("wti_threshold", defaults["wti_threshold"])),
            "max_velocity_z": float(wk.get("max_velocity_z", defaults["max_velocity_z"])),
        }
    except Exception:
        LOG.exception("wash calibration load failed; using fail-closed thresholds")
        return dict(_WASH_CALIBRATION_FAIL_CLOSED)


def wash_block_reason(
    *,
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
    wash_z_threshold: float | None = None,
    wti_threshold: float | None = None,
) -> str | None:
    cuts = _wash_calibrated_thresholds()
    wash_z_threshold = float(
        wash_z_threshold if wash_z_threshold is not None else cuts["wash_z_threshold"]
    )
    wti_threshold = float(
        wti_threshold if wti_threshold is not None else cuts["wti_threshold"]
    )
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    tf15 = (row.get("timeframes") or {}).get("15m") or {}
    quote_vol = _safe_float(
        row.get("quote_volume")
        or market.get("quote_volume_24h")
        or tf15.get("quote_volume")
    )
    baseline_real = _has_real_quote_vol_baseline(market=market, row=row)
    # Honest baseline: skip wash block when baseline is absent (not fail-closed to 0).
    if not baseline_real:
        if quote_vol == 0.0 and _quote_volume_fields_present(market=market, row=row, tf15=tf15):
            return "wash_data_missing"
        return None

    baseline = _safe_float(
        market.get("quote_vol_baseline") or row.get("quote_vol_baseline"),
        0.0,
    )
    if baseline <= 0:
        return None

    chg_24h = _safe_float(row.get("chg_24h_pct") or row.get("change_24h_pct"))
    vol_z = wash_volume_z_score(quote_volume=quote_vol, baseline_volume=baseline)
    wti = wash_trading_index(price_change_pct=chg_24h, volume_z=vol_z)

    if vol_z >= wash_z_threshold and abs(chg_24h) < 2.0:
        return "wash_trading"
    if wti >= wti_threshold:
        return "wash_trading_wti"

    ms = row.get("microstructure") if isinstance(row.get("microstructure"), dict) else {}
    if ms.get("wash_flag") or ms.get("manipulation_wash"):
        return "wash_trading_ms"

    _ = lifecycle
    return None


def kinematic_block_reason(
    *,
    row: dict[str, Any],
    direction: str = "",
    lifecycle_phase: str = "",
    max_velocity_z: float | None = None,
) -> str | None:
    if direction.lower().strip() == "short" and str(lifecycle_phase) in _KINEMATIC_EXEMPT_SHORT_PHASES:
        return None
    cuts = _wash_calibrated_thresholds()
    max_velocity_z = float(
        max_velocity_z if max_velocity_z is not None else cuts["max_velocity_z"]
    )
    tf1h = (row.get("timeframes") or {}).get("1h") or {}
    chg_1h_raw = _optional_float(tf1h.get("change_pct") or tf1h.get("price_change_pct"))
    chg_24h_raw = _optional_float(row.get("chg_24h_pct") or row.get("change_24h_pct"))
    if chg_1h_raw is None and chg_24h_raw is None:
        return "kinematic_data_missing"
    chg_1h = chg_1h_raw if chg_1h_raw is not None else 0.0
    chg_24h = chg_24h_raw if chg_24h_raw is not None else 0.0
    v_z, _ = kinematic_z(change_1h_pct=chg_1h, change_24h_pct=chg_24h)
    phase = str(lifecycle_phase)
    if direction.lower().strip() == "long" and phase in _KINEMATIC_EXEMPT_LONG_PHASES:
        if v_z >= max_velocity_z:
            return "kinematic_chase"
        return None
    if abs(v_z) >= max_velocity_z:
        return "kinematic_chase"
    return None


def fusion_window_wash_abstain(window: Any) -> str | None:
    """Upstream wash check before factor z-scoring (uses window columns only)."""
    vol_ratio = window.last("volume_ratio20")
    chg = window.last("chg_24h_pct")
    if vol_ratio is None or vol_ratio <= 0:
        return None
    vol_z = wash_volume_z_score(
        quote_volume=vol_ratio,
        baseline_volume=1.0,
    )
    price_chg = float(chg or 0.0)
    wti = wash_trading_index(price_change_pct=price_chg, volume_z=vol_z)
    cuts = _wash_calibrated_thresholds()
    if wti >= cuts["wti_threshold"]:
        return "wash_trading_pre_fusion"
    return None


__all__ = [
    "fusion_window_wash_abstain",
    "kinematic_block_reason",
    "kinematic_z",
    "pump_dump_stage",
    "wash_block_reason",
    "wash_trading_index",
    "wash_volume_z_score",
]

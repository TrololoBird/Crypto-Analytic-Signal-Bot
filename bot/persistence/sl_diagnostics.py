"""Stop-loss root-cause classification for outcomes and dashboard analytics."""

from __future__ import annotations

from typing import Any, Literal

CauseOfSL = Literal["timing", "regime", "stop_placement", "thesis"]

_TIMING_CAUSES = frozenset(
    {
        "immediate_adverse_entry",
        "bounce_phase_short_timing",
        "late_activation_timing",
        "quick_stop_no_follow_through",
    }
)
_REGIME_CAUSES = frozenset(
    {
        "bear_long_immediate_stop",
        "bear_long_countertrend",
    }
)
_STOP_PLACEMENT_CAUSES = frozenset(
    {
        "wide_volatility_stop",
        "stop_hunt_post_recovery",
    }
)


def classify_cause_of_sl(code: str) -> CauseOfSL:
    """Map detailed SL root-cause code to answers50 Q26 taxonomy."""
    normalized = str(code or "").strip().lower()
    if normalized in _TIMING_CAUSES:
        return "timing"
    if normalized in _REGIME_CAUSES:
        return "regime"
    if normalized in _STOP_PLACEMENT_CAUSES:
        return "stop_placement"
    return "thesis"


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _feature_float(features: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in features and features[key] is not None:
            parsed = _f(features[key])
            if parsed is not None:
                return parsed
    return None


_SL_OUTCOME_RESULTS = frozenset({"stop_loss", "breakeven_stop", "trailing_stop"})


def classify_stop_loss_root_cause(
    *,
    direction: str,
    mfe: float,
    mae: float,
    time_to_entry_min: int,
    time_to_exit_min: int,
    features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return structured SL diagnosis stored inside outcome features."""
    feat = features or {}
    dir_norm = str(direction or "").lower()
    regime = str(
        feat.get("market_regime")
        or feat.get("regime_1h")
        or feat.get("market_regime_at_close")
        or ""
    ).lower()
    regime_at_close = str(feat.get("market_regime_at_close") or "").lower()
    regime_at_entry = str(feat.get("market_regime") or feat.get("regime_1h") or "").lower()
    btc_bias = str(feat.get("btc_bias") or "").lower()
    bias_4h = str(feat.get("bias_4h") or "").lower()
    atr_pct = _feature_float(feat, "atr_pct", "atr_pct_15m")
    spread_bps = _feature_float(feat, "spread_bps")
    post_sl_fav = _feature_float(feat, "post_sl_favorable_pct")
    post_sl_room = _feature_float(feat, "post_sl_tp1_room_pct")
    score = _feature_float(feat, "score", "base_score")

    reasons: list[str] = []
    code = "thesis_failed"

    activation_lag_s = _feature_float(feat, "activation_lag_seconds")
    bias_1h = str(feat.get("bias_1h") or "").lower()

    if mfe <= 0.0:
        code = "immediate_adverse_entry"
        reasons.append("mfe_zero_price_never_favorable")

    if dir_norm == "short" and mfe <= 0.05 and bias_1h in {"neutral", "uptrend"}:
        code = "bounce_phase_short_timing"
        reasons.append("short_into_1h_bounce_or_neutral")

    if activation_lag_s is not None and activation_lag_s > 300 and mfe <= 0.05:
        reasons.append(f"activation_lag_{int(activation_lag_s)}s")
        if code == "immediate_adverse_entry":
            code = "late_activation_timing"

    bearish = (
        regime in {"bear", "decline", "risk_off"}
        or btc_bias
        in {
            "downtrend",
            "bear",
        }
        or bias_4h == "downtrend"
    )
    if dir_norm == "long" and bearish:
        if code == "immediate_adverse_entry":
            code = "bear_long_immediate_stop"
        else:
            code = "bear_long_countertrend"
        reasons.append("long_against_bear_regime_or_btc_bias")

    if atr_pct is not None and atr_pct >= 1.8:
        reasons.append("elevated_atr_volatility")
        if code == "thesis_failed":
            code = "wide_volatility_stop"

    if spread_bps is not None and spread_bps >= 12.0:
        reasons.append("wide_spread_at_entry")

    active_minutes = max(0, time_to_exit_min - time_to_entry_min)
    if active_minutes <= 5 and mfe <= 0.05:
        reasons.append("stop_within_5m_of_entry")
        if "bear" not in code:
            code = "quick_stop_no_follow_through"

    if regime_at_entry and regime_at_close and regime_at_entry != regime_at_close:
        reasons.append(f"regime_shift_{regime_at_entry}_to_{regime_at_close}")

    if post_sl_fav is not None and post_sl_fav >= 1.0:
        reasons.append(f"post_sl_favorable_{post_sl_fav:.1f}pct")
        if post_sl_room is not None and post_sl_room > 1.5:
            code = "stop_hunt_post_recovery"
            reasons.append("thesis_room_remained_after_stop")

    if score is not None and score < 0.58:
        reasons.append("low_entry_score")

    labels = {
        "immediate_adverse_entry": "Вход сразу против движения (MFE≈0)",
        "bear_long_immediate_stop": "Long в bear - мгновенный стоп без профита",
        "bear_long_countertrend": "Long против bear/BTC↓",
        "wide_volatility_stop": "Стоп на фоне высокой волатильности (ATR%)",
        "quick_stop_no_follow_through": "Быстрый стоп без follow-through",
        "stop_hunt_post_recovery": "Stop hunt - после SL цена шла к TP1",
        "thesis_failed": "Тезис не реализовался (обычный SL)",
        "bounce_phase_short_timing": "Шорт в фазе 1H-отскока (MFE≈0)",
        "late_activation_timing": "Поздняя активация limit-entry",
    }
    cause_of_sl = classify_cause_of_sl(code)
    return {
        "code": code,
        "cause_of_sl": cause_of_sl,
        "label": labels.get(code, code),
        "reasons": reasons,
        "metrics": {
            "mfe": round(mfe, 4),
            "mae": round(mae, 4),
            "time_to_entry_min": int(time_to_entry_min),
            "time_to_exit_min": int(time_to_exit_min),
            "active_minutes": active_minutes,
            "activation_lag_seconds": activation_lag_s,
            "post_sl_favorable_pct": post_sl_fav,
            "post_sl_tp1_room_pct": post_sl_room,
        },
    }


def reclassify_sl_outcomes(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-run SL root-cause classification on persisted stop-loss outcome rows."""
    updated: list[dict[str, Any]] = []
    for row in outcomes:
        result = str(row.get("result") or "").strip().lower()
        if result not in _SL_OUTCOME_RESULTS:
            updated.append(row)
            continue
        features = dict(row.get("features") or {})
        sl_diag = classify_stop_loss_root_cause(
            direction=str(row.get("direction") or ""),
            mfe=float(row.get("mfe") or 0.0),
            mae=float(row.get("mae") or 0.0),
            time_to_entry_min=int(row.get("time_to_entry_min") or 0),
            time_to_exit_min=int(row.get("time_to_exit_min") or 0),
            features=features,
        )
        features["sl_root_cause"] = sl_diag["code"]
        features["cause_of_sl"] = sl_diag.get("cause_of_sl")
        features["sl_root_cause_label"] = sl_diag["label"]
        features["sl_diagnostics"] = sl_diag
        updated.append({**row, "features": features})
    return updated

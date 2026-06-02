"""price_velocity — strategy module (bot/strategies/)."""

from __future__ import annotations

from ..setups.spec_runtime import SpecDetectorSetup


import polars as pl

from ._common import SpecHit, as_float, with_spec_columns

__all__ = ["detect_price_velocity"]
from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setups import _build_signal, _compute_dynamic_score, _reject
import math
from ..setups.spec_runtime import run_setup_detection


def detect_price_velocity(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    lookback: int = 5,
    threshold: float = 0.5,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < lookback + 20:
        return None
    close = as_float(work.item(-1, "close"))
    prior = as_float(work.item(-1 - lookback, "close"))
    atr = as_float(work.item(-1, "spec_atr14"))
    if min(close, prior, atr) <= 0.0:
        return None
    velocity_norm = ((close - prior) / lookback) / atr
    if abs(velocity_norm) <= threshold:
        return None
    direction = "long" if velocity_norm > 0.0 else "short"
    return SpecHit(
        strategy="price_velocity",
        direction=direction,
        entry=close,
        stop_basis=as_float(work.item(-1, "low" if direction == "long" else "high")),
        atr=atr,
        timeframe=timeframe,
        reasons=(f"velocity_norm={velocity_norm:.2f}",),
        structure_clarity=min(1.0, abs(velocity_norm)),
        vol_ratio=as_float(work.item(-1, "volume_ratio20"), 1.0),
        rsi=as_float(work.item(-1, "rsi14"), 50.0),
    )


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    return default


def _finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _orderflow_against_direction(
    prepared: PreparedSymbol,
    direction: str,
    *,
    max_adverse_depth: float,
    max_adverse_micro: float,
) -> tuple[bool, dict[str, float]]:
    depth = _finite_or_none(prepared.depth_imbalance)
    micro = _finite_or_none(prepared.microprice_bias)
    details: dict[str, float] = {}
    if depth is not None:
        details["depth_imbalance"] = depth
    if micro is not None:
        details["microprice_bias"] = micro

    if direction == "long":
        adverse_depth = depth is not None and depth <= -max_adverse_depth
        adverse_micro = micro is not None and micro <= -max_adverse_micro
    else:
        adverse_depth = depth is not None and depth >= max_adverse_depth
        adverse_micro = micro is not None and micro >= max_adverse_micro
    return bool(adverse_depth or adverse_micro), details


def _spec_detect_kwargs(effective: dict[str, float]) -> dict[str, object]:
    return {
        "lookback": int(effective.get("velocity_lookback", 5)),
        "threshold": float(effective.get("velocity_norm_threshold", 0.5)),
    }


def _detect_price_velocity_extended(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    effective_params = effective
    work = prepared.work_15m
    if work.height < 30:
        _reject(prepared, setup_id, "insufficient_15m_bars")
        return None

    required = (
        "open",
        "high",
        "low",
        "close",
        "atr14",
        "roc10",
        "volume_ratio20",
        "close_position",
        "rsi14",
    )
    missing = [column for column in required if column not in work.columns]
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None

    open_ = _as_float(work.item(-1, "open"))
    high = _as_float(work.item(-1, "high"))
    low = _as_float(work.item(-1, "low"))
    close = _as_float(work.item(-1, "close"))
    atr = _as_float(work.item(-1, "atr14"))
    roc10 = _as_float(work.item(-1, "roc10"))
    ols_slope_atr20 = (
        _as_float(work.item(-1, "close_ols_slope_atr20"))
        if "close_ols_slope_atr20" in work.columns
        else 0.0
    )
    vol_ratio = _as_float(work.item(-1, "volume_ratio20"), 1.0)
    close_position = _as_float(work.item(-1, "close_position"), 0.5)
    rsi = _as_float(work.item(-1, "rsi14"), 50.0)

    if min(open_, high, low, close, atr) <= 0.0 or math.isnan(atr):
        _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
        return None

    min_roc = float(effective_params["min_roc10_abs_pct"])
    body_atr = abs(close - open_) / atr
    regression_impulse = abs(ols_slope_atr20) * 2.5
    if (
        abs(roc10) < min_roc
        and body_atr < float(effective_params["min_body_atr"])
        and regression_impulse < min_roc * 0.75
    ):
        _reject(
            prepared,
            setup_id,
            "velocity_too_low",
            roc10=roc10,
            body_atr=body_atr,
            close_ols_slope_atr20=ols_slope_atr20,
        )
        return None
    volume_penalty = vol_ratio < float(effective_params["min_volume_ratio"])

    direction: str | None = None
    directional_velocity = roc10 if abs(roc10) >= min_roc * 0.5 else ols_slope_atr20
    if directional_velocity > 0.0 and close > open_ and close_position >= 0.65:
        direction = "long"
    elif directional_velocity < 0.0 and close < open_ and close_position <= 0.35:
        direction = "short"

    if direction is None:
        if directional_velocity > 0.0 and close_position >= 0.55:
            direction = "long"
        elif directional_velocity < 0.0 and close_position <= 0.45:
            direction = "short"
        else:
            _reject(prepared, setup_id, "direction_not_confirmed", rsi=rsi)
            return None

    adx_1h = (
        _as_float(prepared.work_1h.item(-1, "adx14"), 0.0)
        if not prepared.work_1h.is_empty() and "adx14" in prepared.work_1h.columns
        else 0.0
    )
    min_adx_1h = float(effective_params.get("min_adx_1h", 0.0))
    adx_penalty = adx_1h < min_adx_1h

    orderflow_conflict, orderflow_details = _orderflow_against_direction(
        prepared,
        direction,
        max_adverse_depth=float(effective_params.get("max_adverse_depth_imbalance", 0.12)),
        max_adverse_micro=float(effective_params.get("max_adverse_microprice_bias", 0.12)),
    )
    orderflow_penalty = bool(orderflow_conflict)

    structure_conflict = False
    if float(effective_params.get("strict_1h_structure", 0.0)) > 0.0:
        structure_1h = str(getattr(prepared, "structure_1h", "") or "")
        regime_1h = str(getattr(prepared, "regime_1h_confirmed", "") or "")
        if direction == "long" and (structure_1h == "downtrend" or regime_1h == "downtrend"):
            structure_conflict = True
        if direction == "short" and (structure_1h == "uptrend" or regime_1h == "uptrend"):
            structure_conflict = True

    bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
    sl_buffer = float(effective_params["sl_buffer_atr"])
    min_rr = float(effective_params["min_rr"])
    candle_mid = (high + low) / 2.0
    price_anchor = min(candle_mid, close) if direction == "long" else max(candle_mid, close)
    if direction == "long":
        stop = min(low, open_) - atr * sl_buffer
        risk = price_anchor - stop
        tp1 = price_anchor + risk * min_rr
        tp2 = price_anchor + risk * max(2.0, min_rr + 0.35)
    else:
        stop = max(high, open_) + atr * sl_buffer
        risk = stop - price_anchor
        tp1 = price_anchor - risk * min_rr
        tp2 = price_anchor - risk * max(2.0, min_rr + 0.35)
    if risk <= 0.0:
        _reject(prepared, setup_id, "invalid_stop", stop=stop, close=price_anchor)
        return None

    base_score = float(effective_params["base_score"])
    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
        structure_clarity=min(abs(roc10) / 2.5, 1.0),
    )

    # Graded bias alignment
    if direction == "long" and bias_1h == "downtrend":
        score *= effective_params.get("bias_mismatch_penalty", 0.75)
    elif direction == "short" and bias_1h == "uptrend":
        score *= effective_params.get("bias_mismatch_penalty", 0.75)
    if structure_conflict:
        score *= effective_params.get("structure_conflict_penalty", 0.82)
    if volume_penalty:
        score *= effective_params.get("volume_penalty", 0.90)
    if adx_penalty:
        score *= effective_params.get("adx_penalty", 0.90)
    if orderflow_penalty:
        score *= effective_params.get("orderflow_penalty", 0.86)

    # RSI extremes graded penalty
    if direction == "long" and rsi > float(effective_params["max_rsi_long"]):
        score *= 0.85
    elif direction == "short" and rsi < float(effective_params["min_rsi_short"]):
        score *= 0.85

    reasons = [
        f"price_velocity_{direction}",
        f"roc10={roc10:.2f}",
        f"ols_slope_atr20={ols_slope_atr20:.3f}",
        f"body_atr={body_atr:.2f}",
        f"vol_ratio={vol_ratio:.2f}",
        f"limit_entry={price_anchor:.4f}",
    ]
    if structure_conflict:
        reasons.append("structure_conflict_penalty")
    if volume_penalty:
        reasons.append(f"volume_penalty={vol_ratio:.2f}")
    if adx_penalty:
        reasons.append(f"adx_penalty={adx_1h:.1f}")
    if orderflow_penalty:
        reasons.append("orderflow_penalty")
        for key, value in orderflow_details.items():
            reasons.append(f"{key}={value:.3f}")
    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe="15m",
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=price_anchor,
        atr=atr,
    )


def detect_price_velocity_setup(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    spec_kwargs = _spec_detect_kwargs(effective)
    return run_setup_detection(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        defaults=defaults,
        effective=effective,
        spec_detect=detect_price_velocity,
        extended_detect=_detect_price_velocity_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = [
    "detect_price_velocity",
    "detect_price_velocity_setup",
    "_detect_price_velocity_extended",
]


class PriceVelocitySetup(SpecDetectorSetup):
    setup_id = "price_velocity"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    DEFAULTS = {
        "base_score": 0.53,
        "min_roc10_abs_pct": 0.75,
        "min_body_atr": 0.55,
        "min_volume_ratio": 1.0,
        "max_rsi_long": 82.0,
        "min_rsi_short": 18.0,
        "sl_buffer_atr": 0.55,
        "min_rr": 1.9,
        "min_adx_1h": 16.0,
        "max_adverse_depth_imbalance": 0.12,
        "max_adverse_microprice_bias": 0.12,
        "strict_1h_structure": 0.0,
    }

    detect_setup = detect_price_velocity_setup


__all__ = ["PriceVelocitySetup"]

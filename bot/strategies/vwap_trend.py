"""vwap_trend - canonical strategy detector."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, ClassVar

from ..features.prepare import _swing_points
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ..setups.utils import build_structural_targets
from ._common import SpecHit, as_float, confirmed_pattern_frame, with_spec_columns

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_vwap_reclaim"]


def detect_vwap_reclaim(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    atr = as_float(work.item(-1, "spec_atr14"))
    if atr <= 0.0:
        return None
    prev_close = as_float(work.item(-2, "close"))
    prev_vwap = as_float(work.item(-2, "vwap"))
    close = as_float(work.item(-1, "close"))
    vwap = as_float(work.item(-1, "vwap"))
    if prev_close < prev_vwap and close > vwap:
        return SpecHit(
            strategy="vwap_trend",
            direction="long",
            entry=vwap,
            stop_basis=min(as_float(work.item(-1, "low")), vwap),
            atr=atr,
            timeframe=timeframe,
            reasons=(f"vwap_reclaim={vwap:.4f}",),
            vol_ratio=as_float(work.item(-1, "volume_ratio20"), 1.0),
            rsi=as_float(work.item(-1, "rsi14"), 50.0),
        )
    if prev_close > prev_vwap and close < vwap:
        return SpecHit(
            strategy="vwap_trend",
            direction="short",
            entry=vwap,
            stop_basis=max(as_float(work.item(-1, "high")), vwap),
            atr=atr,
            timeframe=timeframe,
            reasons=(f"vwap_reject={vwap:.4f}",),
            vol_ratio=as_float(work.item(-1, "volume_ratio20"), 1.0),
            rsi=as_float(work.item(-1, "rsi14"), 50.0),
        )
    return None


detect_vwap_trend = detect_vwap_reclaim


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _detect_vwap_trend_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    _defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    work_15m = confirmed_pattern_frame(prepared.work_15m)
    work_1h = confirmed_pattern_frame(prepared.work_1h)
    effective_params = effective
    if work_15m.height < 30 or work_1h.height < 30:
        _reject(prepared, setup_id, "insufficient_bars")
        return None

    required_columns = (
        "close",
        "ema20",
        "vwap",
        "atr14",
        "volume_ratio20",
        "rsi14",
    )
    missing = [column for column in required_columns if column not in work_15m.columns]
    if "adx14" not in work_1h.columns:
        missing.append("adx14")
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None

    close = _as_float(work_15m.item(-1, "close"))
    prev_close = _as_float(work_15m.item(-2, "close"))
    vwap = _as_float(work_15m.item(-1, "vwap"))
    prev_vwap = _as_float(work_15m.item(-2, "vwap"))
    ema20 = _as_float(work_15m.item(-1, "ema20"))
    atr = _as_float(work_15m.item(-1, "atr14"))
    vol_ratio = _as_float(work_15m.item(-1, "volume_ratio20"), 1.0)
    rsi = _as_float(work_15m.item(-1, "rsi14"), 50.0)
    adx_1h = _as_float(work_1h.item(-1, "adx14"))

    if min(close, prev_close, vwap, prev_vwap, ema20, atr) <= 0.0 or math.isnan(atr):
        _reject(
            prepared,
            setup_id,
            "invalid_indicator_state",
            close=close,
            vwap=vwap,
            ema20=ema20,
            atr=atr,
        )
        return None

    tolerance = float(effective_params["vwap_reclaim_tolerance_pct"])
    max_distance_atr = float(effective_params.get("max_vwap_distance_atr", 1.35))
    bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
    direction: str | None = None
    reclaim_lag = 0
    structure_clarity = 0.45
    if (
        prev_close <= prev_vwap * (1.0 + tolerance)
        and close > vwap * (1.0 + tolerance)
        and close > ema20
    ):
        direction = "long"
    elif (
        prev_close >= prev_vwap * (1.0 - tolerance)
        and close < vwap * (1.0 - tolerance)
        and close < ema20
    ):
        direction = "short"

    if direction is None:
        lookback = max(2, int(effective_params.get("reclaim_lookback_bars", 6)))
        recent = work_15m.tail(min(lookback, work_15m.height))
        for local_idx in range(recent.height - 1, -1, -1):
            bar_close = _as_float(recent.item(local_idx, "close"))
            bar_vwap = _as_float(recent.item(local_idx, "vwap"))
            bar_ema20 = _as_float(recent.item(local_idx, "ema20"))
            bar_low = (
                _as_float(recent.item(local_idx, "low")) if "low" in recent.columns else bar_close
            )
            bar_high = (
                _as_float(recent.item(local_idx, "high")) if "high" in recent.columns else bar_close
            )
            if min(bar_close, bar_vwap, bar_ema20) <= 0.0:
                continue
            reclaim_lag = recent.height - 1 - local_idx
            distance_ok = abs(close - vwap) <= atr * max_distance_atr
            if (
                close > vwap * (1.0 + tolerance)
                and close > ema20
                and distance_ok
                and (
                    bar_low <= bar_vwap * (1.0 + tolerance)
                    or bar_close <= bar_vwap * (1.0 + tolerance)
                )
            ):
                direction = "long"
                structure_clarity = max(
                    0.50,
                    1.0 - abs(close - vwap) / max(atr * max_distance_atr, atr),
                )
                break
            if (
                close < vwap * (1.0 - tolerance)
                and close < ema20
                and distance_ok
                and (
                    bar_high >= bar_vwap * (1.0 - tolerance)
                    or bar_close >= bar_vwap * (1.0 - tolerance)
                )
            ):
                direction = "short"
                structure_clarity = max(
                    0.50,
                    1.0 - abs(close - vwap) / max(atr * max_distance_atr, atr),
                )
                break

    if direction is None:
        _reject(prepared, setup_id, "no_vwap_reclaim", close=close, vwap=vwap)
        return None

    entry_price = vwap
    sh_mask, sl_mask = _swing_points(work_1h, n=3, include_unconfirmed_tail=True)
    stop_basis = min(vwap, ema20) if direction == "long" else max(vwap, ema20)
    stop, tp1, tp2 = build_structural_targets(
        direction=direction,
        price_anchor=entry_price,
        stop_basis=stop_basis,
        atr=atr,
        work_1h=work_1h,
        work_4h=prepared.work_4h,
        min_rr=float(effective_params["min_rr"]),
        sl_buffer_atr=float(effective_params["sl_buffer_atr"]),
        sh_mask=sh_mask,
        sl_mask=sl_mask,
    )
    risk = abs(entry_price - stop)
    if risk <= 0.0:
        _reject(prepared, setup_id, "invalid_stop", stop=stop, close=entry_price)
        return None
    min_rr = float(effective_params["min_rr"])
    if tp1 is None or abs(tp1 - entry_price) < risk * min_rr:
        tp1 = entry_price + risk * min_rr if direction == "long" else entry_price - risk * min_rr
    if tp2 is None or abs(tp2 - entry_price) <= abs(tp1 - entry_price):
        tp2 = (
            entry_price + risk * max(2.0, min_rr + 0.35)
            if direction == "long"
            else entry_price - risk * max(2.0, min_rr + 0.35)
        )

    base_score = float(effective_params["base_score"])
    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
        structure_clarity=structure_clarity,
    )

    if adx_1h > 0.0 and adx_1h < float(effective_params["min_adx_1h"]):
        score *= float(effective_params.get("adx_penalty", 0.90))
    if vol_ratio < float(effective_params["min_volume_ratio"]):
        score *= float(effective_params.get("volume_penalty", 0.90))

    # Graded bias alignment
    if (direction == "long" and bias_1h == "downtrend") or (
        direction == "short" and bias_1h == "uptrend"
    ):
        score *= effective_params.get("bias_mismatch_penalty", 0.75)

    ha_boost = 1.0
    if "ha_low" in work_15m.columns and "ha_open" in work_15m.columns:
        ha_low = _as_float(work_15m["ha_low"][-1])
        ha_open = _as_float(work_15m["ha_open"][-1])
        ha_close = _as_float(work_15m["ha_close"][-1])
        ha_high = _as_float(work_15m["ha_high"][-1])
        if direction == "long":
            if ha_low == ha_open and ha_close > ha_open:
                ha_boost = 1.05
        elif ha_high == ha_open and ha_close < ha_open:
            ha_boost = 1.05
    score = min(1.0, score * ha_boost)

    reasons = [
        f"vwap_reclaim_{direction}",
        f"bias_1h={bias_1h}",
        f"vwap={vwap:.4f}",
        f"limit_entry={entry_price:.4f}",
        f"vol_ratio={vol_ratio:.2f}",
        f"adx_1h={adx_1h:.1f}",
        f"reclaim_lag={reclaim_lag}",
    ]
    if ha_boost > 1.0:
        reasons.append("heikin_ashi_confirmed")
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
        price_anchor=entry_price,
        atr=atr,
    )


def detect_vwap_trend_setup(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    spec_kwargs = None
    return run_setup_detection(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        defaults=defaults,
        effective=effective,
        spec_detect=detect_vwap_reclaim,
        extended_detect=_detect_vwap_trend_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = ["_detect_vwap_trend_extended", "detect_vwap_reclaim", "detect_vwap_trend_setup"]


class VWAPTrendSetup(SpecDetectorSetup):
    setup_id = "vwap_trend"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.55,
        "min_adx_1h": 15.0,
        "min_volume_ratio": 1.05,
        "vwap_reclaim_tolerance_pct": 0.0008,
        "reclaim_lookback_bars": 6,
        "max_vwap_distance_atr": 1.35,
        "volume_penalty": 0.9,
        "adx_penalty": 0.9,
        "min_rr": 1.9,
        "sl_buffer_atr": 0.7,
    }

    detect_setup = detect_vwap_trend_setup


__all__ = ["VWAPTrendSetup"]

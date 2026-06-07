"""ema_bounce - canonical strategy detector."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..features.prepare import _swing_points as _sp
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ..setups.utils import build_structural_targets, validate_rr_or_penalty
from ._common import SpecHit, _latest_values, as_float, confirmed_pattern_frame, with_spec_columns

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_ema_bounce", "detect_ema_bounce_setup"]


def detect_ema_bounce(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 45:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0 or row.get("spec_body_ratio", 0.0) <= 0.35:
        return None
    ema200 = row.get("spec_ema200", 0.0) or row.get("ema200", 0.0)
    for period in (9, 21, 50, 200):
        ema = row.get(f"spec_ema{period}", 0.0)
        if ema <= 0.0:
            continue
        if ema200 > 0.0 and row["close"] > ema200 and row["low"] <= ema and row["close"] > ema:
            return SpecHit(
                strategy="ema_bounce",
                direction="long",
                entry=ema,
                stop_basis=row["low"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ema{period}_bounce", f"body_ratio={row['spec_body_ratio']:.2f}"),
                vol_ratio=row.get("volume_ratio20", 1.0),
                rsi=row.get("rsi14", 50.0),
            )
        if ema200 > 0.0 and row["close"] < ema200 and row["high"] >= ema and row["close"] < ema:
            return SpecHit(
                strategy="ema_bounce",
                direction="short",
                entry=ema,
                stop_basis=row["high"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ema{period}_bounce", f"body_ratio={row['spec_body_ratio']:.2f}"),
                vol_ratio=row.get("volume_ratio20", 1.0),
                rsi=row.get("rsi14", 50.0),
            )
    return None


def _detect_ema_bounce_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    ema_touch_tolerance_pct = float(
        dynamic_params.get(
            "ema_touch_tolerance_pct",
            dynamic_params.get(
                "ema_touch_tolerance",
                defaults.get(
                    "ema_touch_tolerance_pct",
                    defaults.get("ema_touch_tolerance", 0.008),
                ),
            ),
        )
    )
    bounce_threshold_atr = float(
        dynamic_params.get(
            "bounce_threshold_atr",
            dynamic_params.get("bounce_threshold_pct", defaults["bounce_threshold_atr"]),
        )
    )
    min_adx = dynamic_params.get(
        "min_adx",
        dynamic_params.get("min_adx_1h", defaults.get("min_adx", defaults["min_adx_1h"])),
    )
    sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults.get("sl_buffer_atr", 1.5)))

    work_1h = confirmed_pattern_frame(prepared.work_1h)
    work_15m = confirmed_pattern_frame(prepared.work_15m)
    context_timeframe = "15m+1h"
    if work_1h.height < 3 or work_15m.height < 5:
        _reject(
            prepared,
            setup_id,
            "insufficient_context_bars",
            bars_1h=work_1h.height,
            bars_15m=work_15m.height,
            required_1h=3,
            required_15m=5,
        )
        return None
    required_1h = {"ema20", "ema50", "close", "adx14"}
    required_15m = {
        "open",
        "high",
        "low",
        "close",
        "atr14",
        "ema20",
        "ema50",
        "volume_ratio20",
        "rsi14",
        "close_position",
    }
    missing_columns = sorted(
        required_1h.difference(work_1h.columns) | required_15m.difference(work_15m.columns)
    )
    if missing_columns:
        _reject(
            prepared,
            setup_id,
            "missing_columns",
            missing_fields=missing_columns,
            context_timeframe=context_timeframe,
        )
        return None

    atr = float(work_15m.item(-1, "atr14") or 0.0)
    ema20_1h = float(work_1h.item(-1, "ema20") or 0.0)
    ema50_1h = float(work_1h.item(-1, "ema50") or 0.0)
    close_1h = float(work_1h.item(-1, "close"))
    ema20 = float(work_15m.item(-1, "ema20") or 0.0)
    ema50 = float(work_15m.item(-1, "ema50") or 0.0)
    open_ = float(work_15m.item(-1, "open") or 0.0)
    high = float(work_15m.item(-1, "high") or 0.0)
    low = float(work_15m.item(-1, "low") or 0.0)
    close = float(work_15m.item(-1, "close"))
    prev_close = float(work_15m.item(-2, "close"))
    close_position = float(work_15m.item(-1, "close_position") or 0.5)

    if min(atr, ema20_1h, ema50_1h, ema20, ema50, open_, high, low, close) <= 0.0:
        _reject(
            prepared,
            setup_id,
            "invalid_indicator_state",
            atr=atr,
            ema20_1h=ema20_1h,
            ema50_1h=ema50_1h,
            ema20_15m=ema20,
            ema50_15m=ema50,
        )
        return None

    reasons: list[str] = []
    bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
    if bias_1h == "neutral":
        roc10 = as_float(work_15m.item(-1, "roc10"), 0.0) if "roc10" in work_15m.columns else 0.0
        if roc10 > 0.20:
            bias_1h = "uptrend"
        elif roc10 < -0.20:
            bias_1h = "downtrend"
    if bias_1h not in {"uptrend", "downtrend"}:
        if close_1h > ema50_1h and ema20_1h > ema50_1h:
            bias_1h = "uptrend"
        elif close_1h < ema50_1h and ema20_1h < ema50_1h:
            bias_1h = "downtrend"

    vol_ratio = float(work_15m.item(-1, "volume_ratio20") or 1.0)
    min_vol = float(dynamic_params.get("min_volume_ratio", defaults.get("min_volume_ratio", 1.0)))

    signal_direction: str | None = None
    recent = work_15m.tail(5)
    if bias_1h == "uptrend":
        recent_low = float(recent["low"].min())
        touch_ema = recent_low <= ema20 * (
            1.0 + float(ema_touch_tolerance_pct)
        ) or recent_low <= ema50 * (1.0 + float(ema_touch_tolerance_pct) * 2.0)
        bounce = (
            close > open_
            and (close - prev_close >= atr * bounce_threshold_atr or close_position >= 0.60)
            and close >= ema20 * (1.0 - float(ema_touch_tolerance_pct))
            and close_position >= 0.55
            and vol_ratio >= min_vol
        )
        if touch_ema and bounce:
            signal_direction = "long"
            reasons = [
                "ema_bounce_long",
                f"context_tf={context_timeframe}",
                f"ema20_1h={ema20_1h:.4f}",
                f"ema50_1h={ema50_1h:.4f}",
                f"ema20_15m={ema20:.4f}",
                f"vol_ratio={vol_ratio:.2f}",
            ]
    elif bias_1h == "downtrend":
        recent_high = float(recent["high"].max())
        touch_ema = recent_high >= ema20 * (
            1.0 - float(ema_touch_tolerance_pct)
        ) or recent_high >= ema50 * (1.0 - float(ema_touch_tolerance_pct) * 2.0)
        bounce = (
            close < open_
            and (prev_close - close >= atr * bounce_threshold_atr or close_position <= 0.40)
            and close <= ema20 * (1.0 + float(ema_touch_tolerance_pct))
            and close_position <= 0.45
            and vol_ratio >= min_vol
        )
        if touch_ema and bounce:
            signal_direction = "short"
            reasons = [
                "ema_bounce_short",
                f"context_tf={context_timeframe}",
                f"ema20_1h={ema20_1h:.4f}",
                f"ema50_1h={ema50_1h:.4f}",
                f"ema20_15m={ema20:.4f}",
                f"vol_ratio={vol_ratio:.2f}",
            ]

    if signal_direction is None:
        _reject(prepared, setup_id, "no_bounce_pattern", bias_1h=bias_1h)
        return None

    rsi = float(work_15m.item(-1, "rsi14") or 50.0)
    adx_1h = float(work_1h.item(-1, "adx14") or 0.0)
    if adx_1h > 0.0 and adx_1h < float(min_adx):
        _reject(prepared, setup_id, "adx_too_low", adx_1h=adx_1h, min_adx=min_adx)
        return None

    sh_mask, sl_mask = _sp(work_1h, n=3, include_unconfirmed_tail=True)
    if signal_direction == "long":
        bounce_ema = min(ema20, ema50) if prev_close <= ema50 * 1.01 else ema20
        price_anchor = min(bounce_ema, close)
    else:
        bounce_ema = max(ema20, ema50) if prev_close >= ema50 * 0.99 else ema20
        price_anchor = max(bounce_ema, close)
    reasons.append(f"limit_entry={price_anchor:.4f}")

    stop, tp1, tp2 = build_structural_targets(
        direction=signal_direction,
        price_anchor=price_anchor,
        stop_basis=bounce_ema,
        atr=atr,
        work_1h=work_1h,
        min_rr=dynamic_params.get("min_rr", defaults["min_rr"]),
        sl_buffer_atr=sl_buffer_atr,
        sh_mask=sh_mask,
        sl_mask=sl_mask,
    )
    if signal_direction == "long" and stop >= price_anchor:
        stop = price_anchor - atr * 0.5
        reasons.append("stop_reanchored_below_entry")
    if signal_direction == "short" and stop <= price_anchor:
        stop = price_anchor + atr * 0.5
        reasons.append("stop_reanchored_above_entry")

    min_rr = dynamic_params.get("min_rr", defaults["min_rr"])
    is_valid_rr, _ = validate_rr_or_penalty(price_anchor, stop, tp1, min_rr)
    base_score = dynamic_params.get("base_score", defaults["base_score"])
    ema_dist_atr = abs(close - bounce_ema) / atr if atr > 0.0 else 1.0
    ha_boost = 1.0
    if "ha_low" in work_15m.columns and "ha_open" in work_15m.columns:
        ha_low_15m = float(work_15m["ha_low"][-1] or 0.0)
        ha_open_15m = float(work_15m["ha_open"][-1] or 0.0)
        ha_close_15m = float(work_15m["ha_close"][-1] or 0.0)
        ha_high_15m = float(work_15m["ha_high"][-1] or 0.0)
        if signal_direction == "long" and ha_low_15m == ha_open_15m and ha_close_15m > ha_open_15m:
            ha_boost = 1.06
        elif signal_direction == "short" and ha_high_15m == ha_open_15m and ha_close_15m < ha_open_15m:
            ha_boost = 1.06

    kama_boost = 1.0
    if "kama10" in work_15m.columns:
        kama_val = float(work_15m["kama10"][-1] or 0.0)
        if kama_val > 0.0:
            if signal_direction == "long" and close > kama_val:
                kama_boost = 1.05
            elif signal_direction == "short" and close < kama_val:
                kama_boost = 1.05

    touch_quality = max(0.0, 1.0 - min(ema_dist_atr / max(bounce_threshold_atr, 0.05), 1.0))
    body_quality = min(1.0, abs(close - open_) / atr / 0.5) if atr > 0.0 else 0.5
    structure_clarity = max(0.35, min(1.0, 0.55 * touch_quality + 0.45 * body_quality))
    score = _compute_dynamic_score(
        direction=signal_direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
        structure_clarity=structure_clarity,
    )
    if not is_valid_rr and tp1 is not None:
        score *= dynamic_params.get("tp_too_close_penalty", defaults["tp_too_close_penalty"])
        reasons.append("tp_too_close_penalty")

    score = min(1.0, score * ha_boost * kama_boost)
    if ha_boost > 1.0:
        reasons.append("heikin_ashi_confirmed")
    if kama_boost > 1.0:
        reasons.append("kama_aligned")

    if tp1 is None:
        risk = abs(price_anchor - stop)
        if risk <= 0.0:
            _reject(
                prepared,
                setup_id,
                "tp1_missing_invalid_risk",
                direction=signal_direction,
                price_anchor=price_anchor,
            )
            return None
        rr_multiplier = float(min_rr)
        if signal_direction == "long":
            tp1 = price_anchor + risk * rr_multiplier
        else:
            tp1 = price_anchor - risk * rr_multiplier
        reasons.append("tp1_atr_fallback")

    if tp2 is None or abs(tp2 - price_anchor) <= abs(tp1 - price_anchor):
        risk = abs(price_anchor - stop)
        tp2 = (
            price_anchor + risk * max(2.0, float(min_rr) + 0.35)
            if signal_direction == "long"
            else price_anchor - risk * max(2.0, float(min_rr) + 0.35)
        )

    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=signal_direction,
        score=score,
        timeframe=context_timeframe,
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=price_anchor,
        atr=atr,
    )


def detect_ema_bounce_setup(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    return run_setup_detection(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        defaults=defaults,
        effective=effective,
        spec_detect=detect_ema_bounce,
        extended_detect=_detect_ema_bounce_extended,
    )


class EmaBounceSetup(SpecDetectorSetup):
    setup_id = "ema_bounce"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.62,
        "min_adx_1h": 15.0,
        "vol_ratio_threshold": 1.0,
        "min_volume_ratio": 1.0,
        "bias_mismatch_penalty": 0.75,
        "tp_too_close_penalty": 0.75,
        "min_rr": 1.9,
        "sl_buffer_atr": 0.5,
        "ema_touch_tolerance_pct": 0.005,
        "ema_touch_tolerance": 0.005,
        "bounce_threshold_atr": 0.12,
    }
    detect_setup = detect_ema_bounce_setup


__all__ = ["EmaBounceSetup"]

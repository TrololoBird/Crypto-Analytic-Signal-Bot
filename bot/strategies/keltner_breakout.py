"""keltner_breakout — canonical strategy detector."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, ClassVar

import polars as pl

from ..features import _swing_points
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ..setups.utils import build_structural_targets
from ._common import SpecHit, _latest_values, as_float, with_spec_columns

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_keltner_breakout"]


def detect_keltner_breakout(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    lookback_bars: int = 3,
    min_volume_ratio: float = 1.30,
    kc_atr_mult: float = 1.80,
    acceptance_band_pct: float = 0.01,
    min_body_ratio: float = 0.45,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    channel_mult = max(1.0, min(float(kc_atr_mult), 3.0))
    work = work.with_columns(
        [
            (pl.col("spec_ema20") + channel_mult * pl.col("spec_atr14")).alias(
                "spec_kc_break_upper"
            ),
            (pl.col("spec_ema20") - channel_mult * pl.col("spec_atr14")).alias(
                "spec_kc_break_lower"
            ),
        ]
    )
    current = _latest_values(work)
    current_close = current.get("close", 0.0)
    current_upper = current.get("spec_kc_break_upper", 0.0)
    current_lower = current.get("spec_kc_break_lower", 0.0)
    current_atr = current.get("spec_atr14", 0.0)
    if min(current_close, current_upper, current_atr) <= 0.0 or current_lower <= 0.0:
        return None
    recent_count = max(1, min(int(lookback_bars), 5, work.height))
    recent = work.tail(recent_count).to_dicts()
    current_idx = int(current.get("_spec_idx", work.height - 1))
    min_vol = max(0.0, float(min_volume_ratio))
    body_floor = max(0.0, min(float(min_body_ratio), 1.0))
    hold_band = max(0.0, min(float(acceptance_band_pct), 0.05))
    current_holds_long = current_close >= current_upper * (1.0 - hold_band)
    current_holds_short = current_close <= current_lower * (1.0 + hold_band)

    for row in reversed(recent):
        idx = int(row.get("_spec_idx", current_idx))
        lag = current_idx - idx
        if lag < 0 or lag >= recent_count:
            continue
        atr = as_float(row.get("spec_atr14"))
        upper = as_float(row.get("spec_kc_break_upper"))
        lower = as_float(row.get("spec_kc_break_lower"))
        volume_mean = as_float(row.get("spec_volume_mean20"))
        volume_ratio = as_float(row.get("volume_ratio20"), 0.0)
        if volume_ratio <= 0.0 and volume_mean > 0.0:
            volume_ratio = as_float(row.get("volume")) / volume_mean
        if atr <= 0.0 or upper <= 0.0 or lower <= 0.0 or volume_ratio < min_vol:
            continue
        body_ratio = as_float(row.get("spec_body_ratio"))
        open_price = as_float(row.get("open"))
        close_price = as_float(row.get("close"))
        directional_body = body_ratio >= body_floor
        if (
            current_holds_long
            and close_price > upper
            and close_price > open_price
            and directional_body
        ):
            return SpecHit(
                strategy="keltner_breakout",
                direction="long",
                entry=max(current_upper, upper),
                stop_basis=as_float(row.get("low")),
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"kc_upper_recent_break={upper:.4f}",
                    f"breakout_lag={lag}",
                    f"kc_mult={channel_mult:.2f}",
                    f"acceptance={current_close / current_upper:.4f}",
                ),
                vol_ratio=volume_ratio,
                rsi=current.get("rsi14", 50.0),
                source_index=idx,
            )
        if (
            current_holds_short
            and close_price < lower
            and close_price < open_price
            and directional_body
        ):
            return SpecHit(
                strategy="keltner_breakout",
                direction="short",
                entry=min(current_lower, lower),
                stop_basis=as_float(row.get("high")),
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"kc_lower_recent_break={lower:.4f}",
                    f"breakout_lag={lag}",
                    f"kc_mult={channel_mult:.2f}",
                    f"acceptance={current_close / current_lower:.4f}",
                ),
                vol_ratio=volume_ratio,
                rsi=current.get("rsi14", 50.0),
                source_index=idx,
            )
    return None


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _spec_detect_kwargs(effective: dict[str, float]) -> dict[str, object]:
    return {
        "lookback_bars": int(effective.get("recent_breakout_bars", 3)),
        "min_volume_ratio": float(effective.get("min_volume_ratio", 1.30)),
        "kc_atr_mult": float(effective.get("kc_atr_mult", 1.80)),
        "acceptance_band_pct": float(effective.get("acceptance_band_pct", 0.01)),
        "min_body_ratio": float(effective.get("min_body_ratio", 0.45)),
    }


def _detect_keltner_breakout_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    _defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    effective_params = effective
    work_15m = prepared.work_15m
    work_1h = prepared.work_1h
    if work_15m.height < 30 or work_1h.height < 30:
        _reject(prepared, setup_id, "insufficient_bars")
        return None

    required = (
        "open",
        "high",
        "low",
        "close",
        "kc_upper",
        "kc_lower",
        "ema20",
        "atr14",
        "volume_ratio20",
        "rsi14",
    )
    missing = [column for column in required if column not in work_15m.columns]
    if "adx14" not in work_1h.columns:
        missing.append("adx14")
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None

    close = _as_float(work_15m.item(-1, "close"))
    ema20 = _as_float(work_15m.item(-1, "ema20"))
    atr = _as_float(work_15m.item(-1, "atr14"))
    vol_ratio = _as_float(work_15m.item(-1, "volume_ratio20"), 1.0)
    rsi = _as_float(work_15m.item(-1, "rsi14"), 50.0)
    adx_1h = _as_float(work_1h.item(-1, "adx14"))

    if min(close, ema20, atr) <= 0.0 or math.isnan(atr):
        _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
        return None

    if adx_1h > 0.0 and adx_1h < float(effective_params["min_adx_1h"]):
        _reject(prepared, setup_id, "adx_too_low", adx_1h=adx_1h)
        return None

    bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
    # Use live 15m momentum to resolve neutral 1h bias before breakout checks.
    if bias_1h == "neutral":
        roc10 = _as_float(work_15m.item(-1, "roc10")) if "roc10" in work_15m.columns else 0.0
        if roc10 > 0.20:
            bias_1h = "uptrend"
        elif roc10 < -0.20:
            bias_1h = "downtrend"
    direction: str | None = None
    stop_basis: float = 0.0
    entry_price = 0.0
    breakout_lag = 0
    breakout_vol_ratio = vol_ratio
    breakout_mode = "close_breakout"
    score_penalty = 1.0
    configured_mult = max(1.0, min(float(effective_params.get("kc_atr_mult", 1.65)), 2.5))
    adaptive_mult = max(
        1.0,
        min(float(effective_params.get("adaptive_kc_atr_mult", 1.45)), configured_mult),
    )
    channel_multipliers = tuple(dict.fromkeys((configured_mult, 1.65, adaptive_mult, 1.45)))
    min_volume_ratio = max(1.0, min(float(effective_params.get("min_volume_ratio", 1.30)), 1.30))
    soft_min_volume_ratio = max(
        1.0,
        min(float(effective_params.get("soft_min_volume_ratio", 1.05)), min_volume_ratio),
    )
    min_body_ratio = max(0.0, min(float(effective_params.get("min_body_ratio", 0.28)), 0.45))
    acceptance_band = max(
        0.0,
        min(float(effective_params.get("acceptance_band_pct", 0.015)), 0.05),
    )
    wick_acceptance_band = max(
        0.0,
        min(float(effective_params.get("wick_acceptance_band_pct", 0.006)), acceptance_band),
    )
    lookback = min(
        max(3, int(effective_params.get("recent_breakout_bars", 5))),
        work_15m.height,
    )
    current_upper = ema20 + configured_mult * atr
    current_lower = ema20 - configured_mult * atr
    best_candidate: dict[str, float | str] | None = None
    for channel_mult in channel_multipliers:
        current_upper_for_mult = ema20 + channel_mult * atr
        current_lower_for_mult = ema20 - channel_mult * atr
        current_holds_long = close >= current_upper_for_mult * (1.0 - acceptance_band)
        current_holds_short = close <= current_lower_for_mult * (1.0 + acceptance_band)
        trend_holds_long = close >= ema20 and close >= current_upper_for_mult * (
            1.0 - acceptance_band * 1.5
        )
        trend_holds_short = close <= ema20 and close <= current_lower_for_mult * (
            1.0 + acceptance_band * 1.5
        )
        for idx in range(work_15m.height - 1, work_15m.height - lookback - 1, -1):
            bar_open = _as_float(work_15m.item(idx, "open"))
            bar_high = _as_float(work_15m.item(idx, "high"))
            bar_low = _as_float(work_15m.item(idx, "low"))
            bar_close = _as_float(work_15m.item(idx, "close"))
            bar_ema = _as_float(work_15m.item(idx, "ema20"))
            bar_atr = _as_float(work_15m.item(idx, "atr14"))
            bar_vol = _as_float(work_15m.item(idx, "volume_ratio20"), 1.0)
            if min(bar_open, bar_high, bar_low, bar_close, bar_ema, bar_atr) <= 0.0:
                continue
            if bar_vol < soft_min_volume_ratio:
                continue
            bar_range = max(bar_high - bar_low, 1e-9)
            body_ratio = abs(bar_close - bar_open) / bar_range
            if body_ratio < min_body_ratio * 0.65 and bar_range < bar_atr * 0.35:
                continue
            bar_upper = bar_ema + channel_mult * bar_atr
            bar_lower = bar_ema - channel_mult * bar_atr
            lag = work_15m.height - 1 - idx
            candidate_direction: str | None = None
            candidate_mode = "close_breakout"
            candidate_entry = 0.0
            candidate_stop_basis = 0.0
            if current_holds_long and bar_close > bar_upper and bar_close > bar_open:
                candidate_direction = "long"
                candidate_entry = max(current_upper_for_mult, bar_upper)
                candidate_stop_basis = min(bar_low, bar_ema)
            elif current_holds_short and bar_close < bar_lower and bar_close < bar_open:
                candidate_direction = "short"
                candidate_entry = min(current_lower_for_mult, bar_lower)
                candidate_stop_basis = max(bar_high, bar_ema)
            elif (
                trend_holds_long
                and bar_high > bar_upper
                and bar_close >= bar_upper * (1.0 - wick_acceptance_band)
                and bar_close >= bar_open
            ):
                candidate_direction = "long"
                candidate_mode = "wick_breakout_acceptance"
                candidate_entry = max(close, current_upper_for_mult * (1.0 - wick_acceptance_band))
                candidate_stop_basis = min(bar_low, bar_ema)
            elif (
                trend_holds_short
                and bar_low < bar_lower
                and bar_close <= bar_lower * (1.0 + wick_acceptance_band)
                and bar_close <= bar_open
            ):
                candidate_direction = "short"
                candidate_mode = "wick_breakout_acceptance"
                candidate_entry = min(close, current_lower_for_mult * (1.0 + wick_acceptance_band))
                candidate_stop_basis = max(bar_high, bar_ema)
            elif (
                trend_holds_long
                and bar_close >= bar_upper * (1.0 - acceptance_band * 1.2)
                and bar_high - bar_low >= bar_atr * 0.45
                and bar_close >= bar_open
            ):
                candidate_direction = "long"
                candidate_mode = "channel_pressure_acceptance"
                candidate_entry = max(close, current_upper_for_mult * (1.0 - acceptance_band))
                candidate_stop_basis = min(bar_low, bar_ema)
            elif (
                trend_holds_short
                and bar_close <= bar_lower * (1.0 + acceptance_band * 1.2)
                and bar_high - bar_low >= bar_atr * 0.45
                and bar_close <= bar_open
            ):
                candidate_direction = "short"
                candidate_mode = "channel_pressure_acceptance"
                candidate_entry = min(close, current_lower_for_mult * (1.0 + acceptance_band))
                candidate_stop_basis = max(bar_high, bar_ema)
            if candidate_direction is None:
                continue
            candidate_penalty = 1.0
            if bar_vol < min_volume_ratio:
                candidate_penalty *= float(effective_params.get("volume_penalty", 0.90))
            if candidate_mode == "wick_breakout_acceptance":
                candidate_penalty *= float(effective_params.get("wick_breakout_penalty", 0.92))
            elif candidate_mode == "channel_pressure_acceptance":
                candidate_penalty *= float(effective_params.get("pressure_breakout_penalty", 0.88))
            quality = (
                (1.75 - channel_mult) * 0.12
                + min(bar_vol / max(min_volume_ratio, 1e-9), 1.8) * 0.30
                + min(body_ratio / max(min_body_ratio, 1e-9), 1.8) * 0.26
                + (lookback - lag) / max(lookback, 1) * 0.20
                + (1.0 if candidate_mode == "close_breakout" else 0.75) * 0.12
            )
            if best_candidate is None or quality > float(best_candidate["quality"]):
                best_candidate = {
                    "direction": candidate_direction,
                    "entry": candidate_entry,
                    "stop_basis": candidate_stop_basis,
                    "lag": float(lag),
                    "vol_ratio": bar_vol,
                    "mult": channel_mult,
                    "penalty": candidate_penalty,
                    "quality": quality,
                    "mode": candidate_mode,
                }

    if best_candidate is not None:
        direction = str(best_candidate["direction"])
        entry_price = float(best_candidate["entry"])
        stop_basis = float(best_candidate["stop_basis"])
        breakout_lag = int(best_candidate["lag"])
        breakout_vol_ratio = float(best_candidate["vol_ratio"])
        kc_atr_mult = float(best_candidate["mult"])
        score_penalty = float(best_candidate["penalty"])
        breakout_mode = str(best_candidate["mode"])
    else:
        kc_atr_mult = configured_mult

    if direction is None:
        _reject(
            prepared,
            setup_id,
            "pattern.no_keltner_breakout",
            close=close,
            kc_upper=current_upper,
            kc_lower=current_lower,
            min_volume_ratio=min_volume_ratio,
            soft_min_volume_ratio=soft_min_volume_ratio,
            lookback=lookback,
        )
        return None

    sh_mask, sl_mask = _swing_points(work_1h, n=3, include_unconfirmed_tail=True)
    min_rr = float(effective_params["min_rr"])
    stop, tp1, tp2 = build_structural_targets(
        direction=direction,
        price_anchor=entry_price,
        stop_basis=stop_basis,
        atr=atr,
        work_1h=work_1h,
        work_4h=prepared.work_4h,
        min_rr=min_rr,
        sl_buffer_atr=float(effective_params["sl_buffer_atr"]),
        sh_mask=sh_mask,
        sl_mask=sl_mask,
    )
    if direction == "long" and stop >= entry_price:
        stop = entry_price - atr * float(effective_params["sl_buffer_atr"])
    elif direction == "short" and stop <= entry_price:
        stop = entry_price + atr * float(effective_params["sl_buffer_atr"])
    risk = abs(entry_price - stop)
    if risk <= 0.0:
        _reject(prepared, setup_id, "invalid_stop", stop=stop, close=entry_price)
        return None
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
        vol_ratio=max(vol_ratio, breakout_vol_ratio),
        rsi=rsi,
        structure_clarity=0.6,
    )
    score *= score_penalty

    # Graded bias alignment
    if (direction == "long" and bias_1h == "downtrend") or (
        direction == "short" and bias_1h == "uptrend"
    ):
        score *= effective_params.get("bias_mismatch_penalty", 0.75)

    reasons = [
        f"keltner_breakout_{direction}",
        f"bias_1h={bias_1h}",
        f"limit_entry={entry_price:.4f}",
        f"breakout_lag={breakout_lag}",
        f"breakout_mode={breakout_mode}",
        f"vol_ratio={vol_ratio:.2f} breakout_vol={breakout_vol_ratio:.2f}",
        f"kc_mult={kc_atr_mult:.2f} acceptance_band={acceptance_band:.3f}",
        f"score_penalty={score_penalty:.2f}",
        f"adx_1h={adx_1h:.1f}",
    ]
    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe="15m+1h",
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=entry_price,
        atr=atr,
    )


def detect_keltner_breakout_setup(
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
        spec_detect=detect_keltner_breakout,
        extended_detect=_detect_keltner_breakout_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = [
    "_detect_keltner_breakout_extended",
    "detect_keltner_breakout",
    "detect_keltner_breakout_setup",
]


class KeltnerBreakoutSetup(SpecDetectorSetup):
    setup_id = "keltner_breakout"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.54,
        "min_volume_ratio": 1.15,
        "min_adx_1h": 13.0,
        "sl_buffer_atr": 0.9,
        "min_rr": 1.9,
        "breakout_lookback_bars": 8,
        "recent_breakout_bars": 5,
        "kc_atr_mult": 1.65,
        "adaptive_kc_atr_mult": 1.45,
        "acceptance_band_pct": 0.015,
        "wick_acceptance_band_pct": 0.006,
        "min_body_ratio": 0.28,
        "soft_min_volume_ratio": 0.95,
        "max_retest_distance_atr": 1.25,
        "volume_penalty": 0.9,
        "wick_breakout_penalty": 0.92,
        "pressure_breakout_penalty": 0.88,
    }

    detect_setup = detect_keltner_breakout_setup


__all__ = ["KeltnerBreakoutSetup"]

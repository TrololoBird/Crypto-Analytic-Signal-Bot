"""fvg_setup — spec detector + SMC zone extended path."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, ClassVar

from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.smc import fvg_candidates, fvg_ce_entry, is_clean_fvg, latest_fvg_zone, swing_series
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ..setups.utils import build_smc_trade_plan, validate_rr_or_penalty
from ._common import SpecHit, as_float, with_spec_columns

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.strategies.fvg")


def detect_fvg(frame: pl.DataFrame, *, timeframe: str = "15m", max_age: int = 20) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 5:
        return None
    current_close = as_float(work.item(-1, "close"))
    current_idx = int(work.item(-1, "_spec_idx"))
    rsi = as_float(work.item(-1, "rsi14"), 50.0)
    fallback_atr = as_float(work.item(-1, "spec_atr14"))
    for idx, direction, bottom, top in fvg_candidates(work, max_age=max_age):
        if not is_clean_fvg(work, created_index=idx, direction=direction):
            continue
        if not (bottom <= current_close <= top):
            continue
        row = work.row(idx, named=True)
        atr = as_float(row.get("spec_atr14"), fallback_atr)
        vol_ratio = as_float(row.get("volume_ratio20"), 1.0)
        age = current_idx - idx
        entry = fvg_ce_entry(bottom=bottom, top=top, direction=direction, price=current_close)
        if direction == "long":
            return SpecHit(
                strategy="fvg_setup",
                direction="long",
                entry=entry,
                stop_basis=bottom,
                atr=atr,
                timeframe=timeframe,
                reasons=(f"bull_fvg zone={bottom:.4f}-{top:.4f}", f"age={age}"),
                vol_ratio=vol_ratio,
                rsi=rsi,
                source_index=idx,
            )
        return SpecHit(
            strategy="fvg_setup",
            direction="short",
            entry=entry,
            stop_basis=top,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"bear_fvg zone={bottom:.4f}-{top:.4f}", f"age={age}"),
            vol_ratio=vol_ratio,
            rsi=rsi,
            source_index=idx,
        )
    return None


def _spec_detect_kwargs(effective: dict[str, float]) -> dict[str, object]:
    return {"max_age": int(effective.get("max_fvg_age_bars", 20))}


def _detect_fvg_setup_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    min_fvg_size_atr = dynamic_params.get("min_fvg_size_atr", defaults["min_fvg_size_atr"])
    min_mitigation_pct = dynamic_params.get("min_mitigation_pct", defaults["min_mitigation_pct"])
    sl_buffer_atr = dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"])

    w = prepared.work_15m
    # FIX 2026-05-21: spec FVG only accepts an immediate retest; fall through
    # to the configured SMC zone scanner before rejecting as no setup.
    if w.height < 5:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w.height)
        return None

    atr = float(w.item(-1, "atr14") or 0.0)
    if atr <= 0 or math.isnan(atr):
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    vol_ratio = float(w.item(-1, "volume_ratio20") or 1.0)
    rsi = float(w.item(-1, "rsi14") or 50.0)

    min_gap_width_bps = dynamic_params.get("min_gap_width_bps", defaults["min_gap_width_bps"])
    min_volume_ratio = dynamic_params.get("min_volume_ratio", defaults["min_volume_ratio"])
    zone = latest_fvg_zone(
        w,
        join_consecutive=True,
        allowed_states=("fresh", "mitigated"),
        current_price=None,
        touch_buffer=0.0,
    )
    if zone is None:
        _reject(prepared, setup_id, "no_fvg_detected")
        return None
    if zone.created_index is None or not is_clean_fvg(
        w,
        created_index=int(zone.created_index),
        direction=zone.direction,
    ):
        _reject(prepared, setup_id, "fvg_not_clean", created_index=zone.created_index)
        return None

    direction = zone.direction
    fvg_low = zone.bottom
    fvg_high = zone.top
    fvg_width = zone.width
    fvg_mid = zone.midpoint
    try:
        zone_values_valid = all(
            math.isfinite(float(value)) and float(value) > 0.0
            for value in (fvg_low, fvg_high, fvg_width, fvg_mid)
        )
    except (TypeError, ValueError):
        zone_values_valid = False
    if (
        direction not in {"long", "short"}
        or zone.created_index is None
        or not (0 <= int(zone.created_index) < w.height)
        or not zone_values_valid
    ):
        _reject(
            prepared,
            setup_id,
            "invalid_fvg_zone",
            direction=direction,
            top=fvg_high,
            bottom=fvg_low,
            width=fvg_width,
            created_index=zone.created_index,
        )
        return None
    price_inside_gap = fvg_low <= price <= fvg_high
    if direction == "long":
        if price < fvg_low:
            _reject(prepared, setup_id, "fvg_already_lost", price=price, bottom=fvg_low)
            return None
        entry_distance = max(0.0, price - fvg_high)
    else:
        if price > fvg_high:
            _reject(prepared, setup_id, "fvg_already_lost", price=price, top=fvg_high)
            return None
        entry_distance = max(0.0, fvg_low - price)
    max_entry_distance = atr * float(
        dynamic_params.get("max_entry_distance_atr", defaults["max_entry_distance_atr"])
    )
    if entry_distance > max_entry_distance:
        _reject(
            prepared,
            setup_id,
            "fvg_retest_too_far",
            entry_distance_atr=entry_distance / atr if atr > 0 else None,
            max_entry_distance_atr=dynamic_params.get(
                "max_entry_distance_atr", defaults["max_entry_distance_atr"]
            ),
        )
        return None
    if price_inside_gap and fvg_width > 0:
        mitigation_pct = (
            (fvg_high - price) / fvg_width if direction == "long" else (price - fvg_low) / fvg_width
        )
        mitigation_pct = max(0.0, min(1.0, mitigation_pct))
    else:
        mitigation_pct = 0.0
    if (
        fvg_width / price < (min_gap_width_bps / 10000)
        or fvg_width < atr * float(min_fvg_size_atr)
        or (price_inside_gap and not (float(min_mitigation_pct) <= mitigation_pct <= 1.0))
    ):
        _reject(
            prepared,
            setup_id,
            "fvg_constraints_failed",
            width=fvg_width,
            mitigation_pct=mitigation_pct,
            price_inside_gap=price_inside_gap,
        )
        return None

    impulse_vol_ratio = 1.0
    if "volume_ratio20" in w.columns:
        try:
            raw_vol_ratio = w.item(zone.created_index, "volume_ratio20")
            impulse_vol_ratio = float(raw_vol_ratio) if raw_vol_ratio is not None else 1.0
        except (IndexError, TypeError, ValueError):
            impulse_vol_ratio = 1.0
    if impulse_vol_ratio < min_volume_ratio:
        _reject(
            prepared,
            setup_id,
            "impulse_volume_below_threshold",
            impulse_vol_ratio=impulse_vol_ratio,
            min_volume_ratio=min_volume_ratio,
        )
        return None

    # Use 1H context for 15M signals (not 4H - too lagging for <4h trades)
    bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
    regime_1h = getattr(prepared, "regime_1h_confirmed", prepared.regime_4h_confirmed)

    # Graded scoring instead of hard reject for bias mismatch
    base_score = dynamic_params.get("base_score", defaults["base_score"])
    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
    )

    if direction == "long" and bias_1h == "downtrend":
        score *= dynamic_params.get("bias_mismatch_penalty", defaults["bias_mismatch_penalty"])
    if direction == "short" and bias_1h == "uptrend":
        score *= dynamic_params.get("bias_mismatch_penalty", defaults["bias_mismatch_penalty"])

    # RSI extremes filter with graded penalty
    rsi_overbought = dynamic_params.get("rsi_overbought", defaults["rsi_overbought"])
    rsi_oversold = dynamic_params.get("rsi_oversold", defaults["rsi_oversold"])
    if direction == "long" and rsi > rsi_overbought:
        score *= 0.85  # Light penalty for overbought
    if direction == "short" and rsi < rsi_oversold:
        score *= 0.85  # Light penalty for oversold

    # 1h structure alignment with graded penalty
    structure_1h = prepared.structure_1h
    if direction == "long" and structure_1h == "downtrend":
        score *= dynamic_params.get("bias_mismatch_penalty", defaults["bias_mismatch_penalty"])
    if direction == "short" and structure_1h == "uptrend":
        score *= dynamic_params.get("bias_mismatch_penalty", defaults["bias_mismatch_penalty"])
    min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
    entry_price = fvg_ce_entry(
        bottom=fvg_low,
        top=fvg_high,
        direction=direction,
        price=price,
    )
    stop_basis = fvg_low if direction == "long" else fvg_high
    pivots = (
        swing_series(prepared.work_1h, swing_length=3, include_unconfirmed_tail=True)
        if prepared.work_1h.height >= 8
        else None
    )
    trade_plan = build_smc_trade_plan(
        direction=direction,
        price_anchor=entry_price,
        stop_basis=stop_basis,
        atr=atr,
        work_1h=prepared.work_1h,
        work_4h=prepared.work_4h,
        min_rr=min_rr,
        sl_buffer_atr=float(sl_buffer_atr),
        sh_mask=pivots.high_mask if pivots is not None else None,
        sl_mask=pivots.low_mask if pivots is not None else None,
    )
    if trade_plan is None:
        _reject(
            prepared,
            setup_id,
            "invalid_stop",
            stop_basis=stop_basis,
            price=entry_price,
        )
        return None
    stop = trade_plan.stop
    tp1 = trade_plan.tp1
    tp2 = trade_plan.tp2
    reasons_note = trade_plan.reasons_note

    is_valid_rr, _ = validate_rr_or_penalty(entry_price, stop, tp1, min_rr)
    if not is_valid_rr and tp1 is not None:
        score *= dynamic_params.get("tp_too_close_penalty", defaults["tp_too_close_penalty"])

    reasons = [
        f"FVG {direction}: gap [{fvg_low:.4f}-{fvg_high:.4f}] state={zone.state}",
        (
            f"price={price:.4f} limit_entry={entry_price:.4f} inside gap "
            f"| entry_distance_atr={entry_distance / atr:.2f} "
            f"| 1h_bias={bias_1h} 1h_struct={structure_1h} 1h_regime={regime_1h}"
        ),
        f"vol_ratio={vol_ratio:.2f} impulse_vol={impulse_vol_ratio:.2f} rsi={rsi:.1f}",
        reasons_note,
    ]

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


def detect_fvg_setup(
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
        spec_detect=detect_fvg,
        extended_detect=_detect_fvg_setup_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = ["_detect_fvg_setup_extended", "detect_fvg", "detect_fvg_setup"]


class FVGSetup(SpecDetectorSetup):
    setup_id = "fvg_setup"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.60,
        "min_gap_width_bps": 15.0,
        "min_volume_ratio": 1.1,
        "bias_mismatch_penalty": 0.75,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "min_rr": 1.9,
        "tp_too_close_penalty": 0.8,
        "min_fvg_size_atr": 0.30,
        "min_mitigation_pct": 0.2,
        "sl_buffer_atr": 0.8,
        "max_entry_distance_atr": 1.5,
    }

    detect_setup = detect_fvg_setup

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        """Tunable parameters for self-learner optimization."""
        defaults = dict(self.DEFAULTS)
        if settings is not None:
            filters = getattr(settings, "filters", None)
            if filters:
                setups_config = getattr(filters, "setups", {})
                if isinstance(setups_config, dict):
                    override = setups_config.get(self.setup_id) or setups_config.get("fvg")
                    if isinstance(override, dict):
                        return {**defaults, **override}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        try:
            return super().detect(prepared, settings)
        except Exception as exc:
            LOG.exception("%s fvg_setup: unexpected error", prepared.symbol)
            _reject(
                prepared,
                self.setup_id,
                "runtime.unexpected_exception",
                stage="runtime",
                exception_type=type(exc).__name__,
            )
            return None


__all__ = ["FVGSetup"]

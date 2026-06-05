"""liquidity_sweep — canonical strategy detector."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, ClassVar

from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.smc import latest_liquidity_sweep, sweep_tolerance, swing_series
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ..setups.utils import build_smc_trade_plan
from ._common import SpecHit, _latest_values, with_spec_columns

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.strategies.liquidity_sweep")

__all__ = ["detect_liquidity_sweep"]


def detect_liquidity_sweep(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    sweep_atr_mult: float = 0.2,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    prev_high = row.get("spec_prev_high20", 0.0)
    prev_low = row.get("spec_prev_low20", 0.0)
    close = row.get("close", 0.0)
    high = row.get("high", 0.0)
    low = row.get("low", 0.0)
    vol_ratio = row.get("volume_ratio20", 1.0)
    rsi = row.get("rsi14", 50.0)
    if atr <= 0.0:
        return None
    high_tol = sweep_tolerance(level=prev_high, atr=atr, sweep_atr_mult=sweep_atr_mult)
    low_tol = sweep_tolerance(level=prev_low, atr=atr, sweep_atr_mult=sweep_atr_mult)
    if high > prev_high + high_tol and close < prev_high and (high - close) > high_tol:
        return SpecHit(
            strategy="liquidity_sweep",
            direction="short",
            entry=prev_high,
            stop_basis=high,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"sweep_high level={prev_high:.4f}", f"wick_atr={(high - close) / atr:.2f}"),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    if low < prev_low - low_tol and close > prev_low and (close - low) > low_tol:
        return SpecHit(
            strategy="liquidity_sweep",
            direction="long",
            entry=prev_low,
            stop_basis=low,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"sweep_low level={prev_low:.4f}", f"wick_atr={(close - low) / atr:.2f}"),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    return None


_SCAN_BARS = 30
_EQUAL_TOL = 0.0015  # 0.15%


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _detect_liquidity_sweep_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    equal_level_tol = float(
        dynamic_params.get(
            "equal_level_tol",
            dynamic_params.get("threshold_tol", defaults["equal_level_tol"]),
        )
    )
    min_level_hits = max(2, int(dynamic_params.get("min_level_hits", defaults["min_level_hits"])))
    sweep_atr_mult = float(dynamic_params.get("sweep_atr_mult", defaults["sweep_atr_mult"]))
    reclaim_threshold = float(
        dynamic_params.get("reclaim_threshold", defaults["reclaim_threshold"])
    )
    sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]))
    min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
    base_score = float(dynamic_params.get("base_score", defaults["base_score"]))
    max_sweep_age_bars = int(
        dynamic_params.get("max_sweep_age_bars", defaults["max_sweep_age_bars"])
    )
    max_entry_distance_atr = float(
        dynamic_params.get("max_entry_distance_atr", defaults["max_entry_distance_atr"])
    )
    w = prepared.work_1h
    if w.height < 10:
        _reject(prepared, setup_id, "insufficient_1h_bars", bars=w.height)
        return None

    atr = float(w.item(-1, "atr14") or 0.0)
    if atr <= 0 or math.isnan(atr):
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    scan = w.tail(_SCAN_BARS) if w.height >= _SCAN_BARS else w
    highs = scan["high"].to_numpy()
    lows = scan["low"].to_numpy()
    closes = scan["close"].to_numpy()
    n = len(scan)

    if n < 3:
        _reject(prepared, setup_id, "scan_window_insufficient", bars=n)
        return None

    zone = latest_liquidity_sweep(
        scan,
        swing_length=max(2, min_level_hits + 1),
        range_percent=equal_level_tol,
    )
    fallback_direction: str | None = None
    fallback_level: float | None = None
    fallback_sweep_index: int | None = None
    fallback_state = ""
    if zone is None or zone.sweep_index is None or zone.state == "invalidated":
        if {"prev_donchian_low20", "prev_donchian_high20"}.issubset(set(scan.columns)):
            first_idx = max(0, scan.height - max_sweep_age_bars - 1)
            for idx in range(scan.height - 1, first_idx - 1, -1):
                prev_low = _as_float(scan.item(idx, "prev_donchian_low20"))
                prev_high = _as_float(scan.item(idx, "prev_donchian_high20"))
                bar_high = _as_float(scan.item(idx, "high"))
                bar_low = _as_float(scan.item(idx, "low"))
                bar_close = _as_float(scan.item(idx, "close"))
                if min(prev_low, prev_high, bar_high, bar_low, bar_close) <= 0.0:
                    continue
                pierce = sweep_tolerance(
                    level=prev_high,
                    atr=atr,
                    sweep_atr_mult=sweep_atr_mult,
                    tolerance_pct=equal_level_tol,
                )
                if bar_high > prev_high + pierce and bar_close < prev_high:
                    fallback_direction = "short"
                    fallback_level = prev_high
                    fallback_sweep_index = idx
                    fallback_state = "donchian_fallback"
                    break
                pierce = sweep_tolerance(
                    level=prev_low,
                    atr=atr,
                    sweep_atr_mult=sweep_atr_mult,
                    tolerance_pct=equal_level_tol,
                )
                if bar_low < prev_low - pierce and bar_close > prev_low:
                    fallback_direction = "long"
                    fallback_level = prev_low
                    fallback_sweep_index = idx
                    fallback_state = "donchian_fallback"
                    break
        if fallback_direction is None or fallback_level is None or fallback_sweep_index is None:
            _reject(prepared, setup_id, "no_liquidity_sweep_detected")
            return None

    direction = (
        zone.direction if zone is not None and zone.sweep_index is not None else fallback_direction
    )
    level = (
        (zone.level or zone.midpoint)
        if zone is not None and zone.sweep_index is not None
        else fallback_level
    )
    zone_state = zone.state if zone is not None and zone.sweep_index is not None else fallback_state
    sweep_index = int(
        zone.sweep_index
        if zone is not None and zone.sweep_index is not None
        else fallback_sweep_index
    )
    if direction not in {"long", "short"} or level is None:
        _reject(prepared, setup_id, "invalid_liquidity_sweep_state")
        return None
    entry_price = float(level)
    if not (0 <= sweep_index < n):
        _reject(
            prepared,
            setup_id,
            "liquidity_sweep_index_out_of_bounds",
            sweep_index=sweep_index,
            bars=n,
        )
        return None
    sweep_age = scan.height - 1 - sweep_index
    if sweep_age > max_sweep_age_bars:
        _reject(
            prepared,
            setup_id,
            "liquidity_sweep_too_old",
            sweep_age=sweep_age,
            max_sweep_age_bars=max_sweep_age_bars,
        )
        return None
    sweep_bar_h = float(highs[sweep_index])
    sweep_bar_l = float(lows[sweep_index])
    sweep_bar_c = float(closes[sweep_index])
    confirmation_close = float(closes[-1])
    if (
        not prepared.work_15m.is_empty()
        and "close" in prepared.work_15m.columns
        and prepared.work_15m.height >= 1
    ):
        confirmation_close = _as_float(
            prepared.work_15m.item(-1, "close"),
            confirmation_close,
        )

    if direction == "short":
        eq_high_level = level
        if eq_high_level is None or not math.isfinite(float(eq_high_level)):
            _reject(prepared, setup_id, "liquidity_level_missing", direction="short")
            return None
        if (
            sweep_bar_h <= eq_high_level
            or sweep_bar_c >= eq_high_level
            or confirmation_close >= eq_high_level + reclaim_threshold * atr
        ):
            _reject(
                prepared,
                setup_id,
                "short_reclaim_not_confirmed",
                level=eq_high_level,
            )
            return None
        if abs(entry_price - confirmation_close) > max_entry_distance_atr * atr:
            _reject(
                prepared,
                setup_id,
                "entry_too_far_from_confirmation",
                price=entry_price,
                close=confirmation_close,
                max_entry_distance_atr=max_entry_distance_atr,
            )
            return None

        pivots = swing_series(w, swing_length=3, include_unconfirmed_tail=True)
        trade_plan = build_smc_trade_plan(
            direction="short",
            price_anchor=entry_price,
            stop_basis=sweep_bar_h,
            atr=atr,
            work_1h=w,
            work_4h=prepared.work_4h,
            min_rr=min_rr,
            sl_buffer_atr=sl_buffer_atr,
            sh_mask=None,
            sl_mask=pivots.low_mask,
        )
        if trade_plan is None:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_short",
                stop_basis=sweep_bar_h,
                price=entry_price,
            )
            return None
        stop = trade_plan.stop
        tp1 = trade_plan.tp1
        tp2 = trade_plan.tp2
        risk = trade_plan.risk
        if tp1 >= entry_price or abs(tp1 - entry_price) + 1e-9 < risk * min_rr:
            _reject(
                prepared,
                setup_id,
                "tp1_too_close_or_missing",
                tp1=tp1,
                risk=risk,
                price=entry_price,
            )
            return None

        vol_ratio = _as_float(w.item(-1, "volume_ratio20"), 1.0)
        rsi = _as_float(w.item(-1, "rsi14"), 50.0)
        score = _compute_dynamic_score(
            direction="short",
            base_score=base_score,
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
        reasons = [
            f"Liquidity sweep short: eq_high={eq_high_level:.4f} state={zone_state}",
            (
                f"wick={sweep_bar_h:.4f} close={sweep_bar_c:.4f} "
                f"confirm={confirmation_close:.4f} age={sweep_age}"
            ),
        ]
        return _build_signal(
            prepared=prepared,
            setup_id=setup_id,
            direction="short",
            score=score,
            timeframe="1h",
            reasons=reasons,
            strategy_family=family,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=entry_price,
            atr=atr,
        )

    eq_low_level = level
    if eq_low_level is None or not math.isfinite(float(eq_low_level)):
        _reject(prepared, setup_id, "liquidity_level_missing", direction="long")
        return None
    if (
        sweep_bar_l >= eq_low_level
        or sweep_bar_c <= eq_low_level
        or confirmation_close <= eq_low_level - reclaim_threshold * atr
    ):
        _reject(prepared, setup_id, "long_reclaim_not_confirmed", level=eq_low_level)
        return None
    if abs(entry_price - confirmation_close) > max_entry_distance_atr * atr:
        _reject(
            prepared,
            setup_id,
            "entry_too_far_from_confirmation",
            price=entry_price,
            close=confirmation_close,
            max_entry_distance_atr=max_entry_distance_atr,
        )
        return None

    pivots = swing_series(w, swing_length=3, include_unconfirmed_tail=True)
    trade_plan = build_smc_trade_plan(
        direction="long",
        price_anchor=entry_price,
        stop_basis=sweep_bar_l,
        atr=atr,
        work_1h=w,
        work_4h=prepared.work_4h,
        min_rr=min_rr,
        sl_buffer_atr=sl_buffer_atr,
        sh_mask=pivots.high_mask,
        sl_mask=None,
    )
    if trade_plan is None:
        _reject(
            prepared, setup_id, "risk_non_positive_long", stop_basis=sweep_bar_l, price=entry_price
        )
        return None
    stop = trade_plan.stop
    tp1 = trade_plan.tp1
    tp2 = trade_plan.tp2
    risk = trade_plan.risk
    if tp1 <= entry_price or abs(tp1 - entry_price) + 1e-9 < risk * min_rr:
        _reject(
            prepared,
            setup_id,
            "tp1_too_close_or_missing",
            tp1=tp1,
            risk=risk,
            price=entry_price,
        )
        return None

    vol_ratio = _as_float(w.item(-1, "volume_ratio20"), 1.0)
    rsi = _as_float(w.item(-1, "rsi14"), 50.0)
    score = _compute_dynamic_score(
        direction="long",
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
    )
    reasons = [
        f"Liquidity sweep long: eq_low={eq_low_level:.4f} state={zone_state}",
        (
            f"wick={sweep_bar_l:.4f} close={sweep_bar_c:.4f} "
            f"confirm={confirmation_close:.4f} age={sweep_age}"
        ),
    ]
    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction="long",
        score=score,
        timeframe="1h",
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=entry_price,
        atr=atr,
    )


def detect_liquidity_sweep_setup(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    spec_kwargs = {
        "sweep_atr_mult": float(effective.get("sweep_atr_mult", defaults["sweep_atr_mult"])),
    }
    return run_setup_detection(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        defaults=defaults,
        effective=effective,
        spec_detect=detect_liquidity_sweep,
        extended_detect=_detect_liquidity_sweep_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = [
    "_detect_liquidity_sweep_extended",
    "detect_liquidity_sweep",
    "detect_liquidity_sweep_setup",
]


class LiquiditySweepSetup(SpecDetectorSetup):
    setup_id = "liquidity_sweep"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.54,
        "equal_level_tol": 0.0015,
        "threshold_tol": 0.0015,
        "min_level_hits": 2,
        "sweep_atr_mult": 0.20,
        "reclaim_threshold": 0.30,
        "max_sweep_age_bars": 4,
        "max_entry_distance_atr": 1.25,
        "sl_buffer_atr": 0.50,
        "bias_mismatch_penalty": 0.75,
        "min_rr": 1.9,
    }

    detect_setup = detect_liquidity_sweep_setup

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        defaults = dict(self.DEFAULTS)
        if settings is not None:
            filters = getattr(settings, "filters", None)
            if filters:
                setups_config = getattr(filters, "setups", {})
                if isinstance(setups_config, dict) and self.setup_id in setups_config:
                    return {**defaults, **setups_config.get(self.setup_id, {})}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        try:
            return super().detect(prepared, settings)
        except Exception as exc:
            LOG.exception("%s liquidity_sweep: unexpected error", prepared.symbol)
            _reject(
                prepared,
                self.setup_id,
                "runtime.unexpected_exception",
                stage="runtime",
                exception_type=type(exc).__name__,
            )
            return None


__all__ = ["LiquiditySweepSetup"]

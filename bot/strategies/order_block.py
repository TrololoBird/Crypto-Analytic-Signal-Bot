"""order_block — canonical strategy detector."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, ClassVar

from ..delivery.trade_plan import TradePlanBuilder
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.smc import latest_liquidity_sweep, latest_order_block, swing_series
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ..setups.utils import build_smc_trade_plan, validate_rr_or_penalty
from ._common import SpecHit, _latest_values, as_float, with_spec_columns

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.strategies.order_block")

__all__ = ["detect_order_block"]


def detect_order_block(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    ob_max_age: int = 72,
    touch_buffer_atr: float = 0.25,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 30:
        return None
    current = _latest_values(work)
    atr = current.get("spec_atr14", 0.0)
    close = current.get("close", 0.0)
    if atr <= 0.0 or close <= 0.0:
        return None
    zone = latest_order_block(
        work,
        swing_length=3,
        include_unconfirmed_tail=True,
        current_price=close,
        touch_buffer=touch_buffer_atr * atr,
    )
    if zone is None:
        return None
    age = work.height - 1 - zone.created_index
    if age > ob_max_age:
        return None
    bottom = as_float(zone.bottom)
    top = as_float(zone.top)
    direction = zone.direction
    return SpecHit(
        strategy="order_block",
        direction=direction,
        entry=(bottom + top) / 2.0,
        stop_basis=bottom if direction == "long" else top,
        atr=atr,
        timeframe=timeframe,
        reasons=(f"ob_zone={bottom:.4f}-{top:.4f}", f"age={age}"),
        structure_clarity=0.76,
        vol_ratio=current.get("volume_ratio20", 1.0),
        rsi=current.get("rsi14", 50.0),
        source_index=int(zone.created_index),
    )


def _spec_detect_kwargs(effective: dict[str, float]) -> dict[str, object]:
    return {
        "ob_max_age": int(effective.get("ob_max_age", 72)),
        "touch_buffer_atr": float(effective.get("touch_buffer_atr", 0.25)),
    }


def _detect_order_block_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults.get("sl_buffer_atr", 1.5)))

    w15m = prepared.work_15m
    w1h = prepared.work_1h
    if w15m.height < 10:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w15m.height)
        return None

    atr = float(w15m.item(-1, "atr14") or 0.0)
    if atr <= 0 or math.isnan(atr):
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    closes = w15m["close"].to_numpy()
    opens = w15m["open"].to_numpy()
    n_bars = w15m.height

    min_ob_impulse_atr = dynamic_params.get("min_ob_impulse_atr", defaults["min_ob_impulse_atr"])
    impulse_lookback = max(
        3,
        int(dynamic_params.get("impulse_lookback_bars", defaults["impulse_lookback_bars"])),
    )
    ob_max_age = dynamic_params.get("ob_max_age", defaults["ob_max_age"])
    touch_buffer_atr = float(dynamic_params.get("touch_buffer_atr", defaults["touch_buffer_atr"]))
    zone = latest_order_block(
        w15m,
        swing_length=3,
        include_unconfirmed_tail=True,
        current_price=price,
        touch_buffer=touch_buffer_atr * atr,
    )
    if zone is None:
        _reject(prepared, setup_id, "no_order_block_detected")
        return None

    direction = zone.direction
    ob_low = zone.bottom
    ob_high = zone.top
    try:
        zone_values_valid = all(
            math.isfinite(float(value)) and float(value) > 0.0 for value in (ob_low, ob_high)
        )
    except (TypeError, ValueError):
        zone_values_valid = False
    if (
        direction not in {"long", "short"}
        or zone.created_index is None
        or not (0 <= int(zone.created_index) < n_bars)
        or not zone_values_valid
    ):
        _reject(
            prepared,
            setup_id,
            "invalid_order_block_zone",
            direction=direction,
            top=ob_high,
            bottom=ob_low,
            created_index=zone.created_index,
        )
        return None
    age = n_bars - 1 - zone.created_index
    if age > ob_max_age:
        _reject(prepared, setup_id, "order_block_too_old", age=age, max_age=ob_max_age)
        return None

    sweep = latest_liquidity_sweep(w15m, swing_length=3)
    if sweep is not None and sweep.direction != direction:
        _reject(
            prepared,
            setup_id,
            "order_block_sweep_mismatch",
            ob_direction=direction,
            sweep_direction=sweep.direction,
        )
        return None

    impulse_start = zone.created_index + 1
    impulse_end = min(impulse_start + impulse_lookback, n_bars)
    impulse_dir = 1 if direction == "long" else -1
    impulse_moves = [
        (closes[k] - opens[k]) * impulse_dir for k in range(impulse_start, impulse_end)
    ]
    if len(impulse_moves) < 2 or sum(1 for move in impulse_moves if move > 0) < 2:
        _reject(
            prepared,
            setup_id,
            "order_block_impulse_missing",
            created_index=zone.created_index,
        )
        return None
    total_move = closes[impulse_end - 1] - opens[impulse_start]
    if abs(total_move) < min_ob_impulse_atr * atr:
        _reject(
            prepared,
            setup_id,
            "order_block_impulse_too_small",
            total_move=total_move,
            min_ob_impulse_atr=min_ob_impulse_atr,
        )
        return None

    # Use 1H context for 15M signals (not 4H - too lagging for <4h trades)
    bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
    structure_1h = prepared.structure_1h
    rsi_check = float(w15m.item(-1, "rsi14") or 50.0)

    min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))

    vol_ratio = float(w15m.item(-1, "volume_ratio20") or 1.0)
    rsi = float(w15m.item(-1, "rsi14") or 50.0)

    base_score = dynamic_params.get("base_score", defaults["base_score"])
    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
    )

    context_mismatch = (
        direction == "long" and (bias_1h == "downtrend" or structure_1h == "downtrend")
    ) or (direction == "short" and (bias_1h == "uptrend" or structure_1h == "uptrend"))
    if context_mismatch:
        score *= dynamic_params.get("bias_mismatch_penalty", defaults["bias_mismatch_penalty"])

    rsi_overbought = dynamic_params.get("rsi_overbought", defaults["rsi_overbought"])
    rsi_oversold = dynamic_params.get("rsi_oversold", defaults["rsi_oversold"])
    if direction == "long" and rsi_check > rsi_overbought:
        score *= 0.85
    if direction == "short" and rsi_check < rsi_oversold:
        score *= 0.85
    if zone.state == "mitigated":
        score *= 0.95

    entry_price = ob_low if direction == "long" else ob_high
    stop_basis = ob_low if direction == "long" else ob_high
    pivots = (
        swing_series(w1h, swing_length=3, include_unconfirmed_tail=True)
        if w1h.height >= 8
        else None
    )
    trade_plan_smc = build_smc_trade_plan(
        direction=direction,
        price_anchor=entry_price,
        stop_basis=stop_basis,
        atr=atr,
        work_1h=w1h,
        work_4h=prepared.work_4h,
        min_rr=min_rr,
        sl_buffer_atr=sl_buffer_atr,
        sh_mask=pivots.high_mask if pivots is not None else None,
        sl_mask=pivots.low_mask if pivots is not None else None,
    )
    if trade_plan_smc is None:
        _reject(prepared, setup_id, "invalid_stop", stop_basis=stop_basis, price=entry_price)
        return None
    stop = trade_plan_smc.stop
    tp1 = trade_plan_smc.tp1
    tp2 = trade_plan_smc.tp2
    risk = trade_plan_smc.risk
    reasons_note = trade_plan_smc.reasons_note

    is_valid_rr, _ = validate_rr_or_penalty(entry_price, stop, tp1, min_rr)
    if not is_valid_rr and tp1 is not None:
        score *= dynamic_params.get("tp_too_close_penalty", defaults["tp_too_close_penalty"])

    trade_plan = TradePlanBuilder.build(
        direction=direction,
        setup_id=setup_id,
        strategy_family=family,
        timeframe="15m",
        price_anchor=entry_price,
        atr=atr,
        stop_loss=stop,
        tp1=tp1,
        tp2=tp2,
    )
    if trade_plan is None:
        _reject(
            prepared,
            setup_id,
            "targets.target_integrity_failed",
            direction=direction,
            price_anchor=entry_price,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
        )
        return None
    stop = trade_plan.stop_loss
    tp1 = trade_plan.tp1
    tp2 = trade_plan.tp2

    reasons = [
        f"OB {direction}: zone [{ob_low:.4f}-{ob_high:.4f}] state={zone.state}",
        (
            f"age={age}bars price={price:.4f} limit_entry={entry_price:.4f} "
            f"| 1h_bias={bias_1h} 1h_struct={structure_1h}"
        ),
        f"rsi={rsi_check:.1f}",
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


def detect_order_block_setup(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    # Two-tier path: fast spec hit via latest_order_block retest; otherwise
    # extended_detect applies impulse validation on the same 15m SMC scan.
    spec_kwargs = _spec_detect_kwargs(effective)
    return run_setup_detection(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        defaults=defaults,
        effective=effective,
        spec_detect=detect_order_block,
        extended_detect=_detect_order_block_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = ["_detect_order_block_extended", "detect_order_block", "detect_order_block_setup"]


class OrderBlockSetup(SpecDetectorSetup):
    setup_id = "order_block"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.52,
        "min_ob_impulse_atr": 1.5,
        "impulse_lookback_bars": 5,
        "ob_max_age": 72.0,
        "touch_buffer_atr": 0.25,
        "bias_mismatch_penalty": 0.75,
        "tp_too_close_penalty": 0.75,
        "min_rr": 1.9,
        "sl_buffer_atr": 0.5,
        "rsi_overbought": 76.0,
        "rsi_oversold": 24.0,
    }

    detect_setup = detect_order_block_setup

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        """Tunable parameters for self-learner optimization."""
        defaults = {
            "base_score": 0.52,
            "min_ob_impulse_atr": 1.5,
            "impulse_lookback_bars": 5,
            "ob_max_age": 72.0,
            "touch_buffer_atr": 0.25,
            "bias_mismatch_penalty": 0.75,
            "tp_too_close_penalty": 0.75,
            "min_rr": 1.9,
            "sl_buffer_atr": 0.5,
            "rsi_overbought": 76.0,
            "rsi_oversold": 24.0,
        }
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
        except ValueError as exc:
            if "Signal contract violations" in str(exc):
                _reject(
                    prepared,
                    self.setup_id,
                    "targets.contract_violation",
                    stage="runtime",
                    detail=str(exc),
                )
                return None
            raise
        except Exception as exc:
            LOG.exception("%s order_block: unexpected error", prepared.symbol)
            _reject(
                prepared,
                self.setup_id,
                "runtime.unexpected_exception",
                stage="runtime",
                exception_type=type(exc).__name__,
            )
            return None


__all__ = ["OrderBlockSetup"]

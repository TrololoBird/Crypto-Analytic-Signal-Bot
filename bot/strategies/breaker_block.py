"""breaker_block - canonical strategy detector."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from ..features.prepare import _swing_points as _sp
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.smc import latest_breaker_block
from ..setups.spec_runtime import run_setup_detection
from ._common import (
    SpecHit,
    _latest_values,
    _valid_order_block_rows,
    as_float,
    confirmed_pattern_frame,
    with_spec_columns,
)

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.strategies.breaker_block")

__all__ = ["detect_breaker_block"]


def detect_breaker_block(frame: pl.DataFrame, *, timeframe: str = "1h") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 35:
        return None
    current = _latest_values(work)
    atr = current.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    rows = work.to_dicts()
    current_low = current["low"]
    current_high = current["high"]
    current_close = current["close"]
    for zone in reversed(_valid_order_block_rows(work, max_age=120)):
        if not bool(zone.get("volume_ok")):
            continue
        direction = str(zone["direction"])
        bottom = as_float(zone.get("bottom"))
        top = as_float(zone.get("top"))
        source_idx = int(zone["_spec_idx"])
        break_rows = [row for row in rows if int(row["_spec_idx"]) > source_idx]
        if direction == "long":
            broken = any(as_float(row.get("close")) < bottom for row in break_rows)
            retested = current_high >= bottom and current_close < top
            if broken and retested:
                return SpecHit(
                    strategy="breaker_block",
                    direction="short",
                    entry=(bottom + top) / 2.0,
                    stop_basis=top,
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(f"bull_ob_flipped_resistance={bottom:.4f}-{top:.4f}",),
                    structure_clarity=0.74,
                    vol_ratio=current.get("volume_ratio20", 1.0),
                    rsi=current.get("rsi14", 50.0),
                    source_index=source_idx,
                )
        else:
            broken = any(as_float(row.get("close")) > top for row in break_rows)
            retested = current_low <= top and current_close > bottom
            if broken and retested:
                return SpecHit(
                    strategy="breaker_block",
                    direction="long",
                    entry=(bottom + top) / 2.0,
                    stop_basis=bottom,
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(f"bear_ob_flipped_support={bottom:.4f}-{top:.4f}",),
                    structure_clarity=0.74,
                    vol_ratio=current.get("volume_ratio20", 1.0),
                    rsi=current.get("rsi14", 50.0),
                    source_index=source_idx,
                )
    return None


_SCAN_BARS = 40


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    return default


def _last(frame: object, column: str, default: float = 0.0) -> float:
    if not hasattr(frame, "is_empty") or frame.is_empty() or column not in frame.columns:
        return default
    return _as_float(frame.item(-1, column), default)


def _detect_breaker_block_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    scan_bars = max(15, int(dynamic_params.get("scan_bars", defaults["scan_bars"])))
    mitigation_threshold = float(
        dynamic_params.get("mitigation_threshold", defaults["mitigation_threshold"])
    )
    sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]))
    min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
    base_score = float(dynamic_params.get("base_score", defaults["base_score"]))
    min_volume_ratio = float(dynamic_params.get("min_volume_ratio", defaults["min_volume_ratio"]))

    w1h = confirmed_pattern_frame(prepared.work_1h)
    w15m = confirmed_pattern_frame(prepared.work_15m)
    if w1h.height < 15:
        _reject(prepared, setup_id, "insufficient_1h_bars", bars=w1h.height)
        return None

    atr = float(w1h.item(-1, "atr14") or 0.0)
    if atr <= 0 or math.isnan(atr):
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    scan = w1h.tail(scan_bars) if w1h.height >= scan_bars else w1h
    zone = latest_breaker_block(
        scan,
        swing_length=3,
        current_price=price,
        retest_buffer=mitigation_threshold * atr,
    )
    if zone is None:
        _reject(prepared, setup_id, "no_breaker_block_detected")
        return None

    direction = zone.direction
    bb_low = zone.bottom
    bb_high = zone.top
    if direction == "long":
        entry_price = bb_high if bb_high <= price else bb_low
    else:
        entry_price = bb_low if bb_low >= price else bb_high

    vol_ratio_15m = _last(w15m, "volume_ratio20", 1.0)
    if vol_ratio_15m < min_volume_ratio:
        _reject(
            prepared,
            setup_id,
            "volume_too_low",
            volume_ratio=vol_ratio_15m,
            min_volume_ratio=min_volume_ratio,
        )
        return None

    close_position = _last(w15m, "close_position", 0.5)
    if direction == "long" and close_position < float(
        dynamic_params.get(
            "min_acceptance_close_position_long",
            defaults["min_acceptance_close_position_long"],
        )
    ):
        _reject(
            prepared,
            setup_id,
            "retest_acceptance_missing",
            direction=direction,
            close_position=close_position,
        )
        return None
    if direction == "short" and close_position > float(
        dynamic_params.get(
            "max_acceptance_close_position_short",
            defaults["max_acceptance_close_position_short"],
        )
    ):
        _reject(
            prepared,
            setup_id,
            "retest_acceptance_missing",
            direction=direction,
            close_position=close_position,
        )
        return None

    # --- Compute structural SL/TP ---
    if direction == "long":
        # SL: beyond breaker block level + sl_buffer_atrxATR.
        stop = bb_low - sl_buffer_atr * atr
        risk = entry_price - stop
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_long",
                stop=stop,
                price=entry_price,
            )
            return None
        # TP1: next 1h swing high (liquidity target / imbalance fill)
        sh_mask, _ = _sp(w1h, n=3, include_unconfirmed_tail=False)
        sh_prices = w1h.filter(sh_mask)["high"]
        tp1_candidates = sh_prices.filter(sh_prices > entry_price)
        tp1 = float(tp1_candidates[0]) if tp1_candidates.len() > 0 else None
        # TP2: 4h structural resistance
        w4h = prepared.work_4h
        tp2 = None
        if w4h is not None and w4h.height > 5:
            sh4_mask, _ = _sp(w4h, n=2)
            sh4_prices = w4h.filter(sh4_mask)["high"]
            tp2_cands = sh4_prices.filter(sh4_prices > entry_price)
            tp2 = float(tp2_cands[0]) if tp2_cands.len() > 0 else None
    else:
        # SL: beyond breaker block level + sl_buffer_atrxATR.
        stop = bb_high + sl_buffer_atr * atr
        risk = stop - entry_price
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_short",
                stop=stop,
                price=entry_price,
            )
            return None
        # TP1: next 1h swing low (liquidity target)
        _, sl_mask = _sp(w1h, n=3, include_unconfirmed_tail=False)
        sl_prices = w1h.filter(sl_mask)["low"]
        tp1_candidates = sl_prices.filter(sl_prices < entry_price)
        tp1 = float(tp1_candidates[-1]) if tp1_candidates.len() > 0 else None
        # TP2: 4h structural support
        w4h = prepared.work_4h
        tp2 = None
        if w4h is not None and w4h.height > 5:
            _, sl4_mask = _sp(w4h, n=2)
            sl4_prices = w4h.filter(sl4_mask)["low"]
            tp2_cands = sl4_prices.filter(sl4_prices < entry_price)
            tp2 = float(tp2_cands[-1]) if tp2_cands.len() > 0 else None

    fallback_note = None
    if tp1 is None or abs(tp1 - entry_price) < risk * min_rr:
        tp1 = entry_price + risk * min_rr if direction == "long" else entry_price - risk * min_rr
        fallback_note = f"tp1_rr_fallback_{min_rr:.2f}"
    if tp2 is None or abs(tp2 - entry_price) <= abs(tp1 - entry_price):
        tp2 = (
            entry_price + risk * max(2.0, min_rr + 0.35)
            if direction == "long"
            else entry_price - risk * max(2.0, min_rr + 0.35)
        )

    vol_ratio = float(w1h.item(-1, "volume_ratio20") or 1.0)
    rsi = float(w1h.item(-1, "rsi14") or 50.0)
    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
    )

    reasons = [
        f"Breaker block {direction}: zone [{bb_low:.4f}-{bb_high:.4f}] state={zone.state}",
        (
            f"price={price:.4f} limit_entry={entry_price:.4f} "
            f"retesting broken OB from {zone.metadata.get('source_ob_direction')}"
        ),
    ]
    if fallback_note:
        reasons.append(fallback_note)

    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
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


def detect_breaker_block_setup(
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
        spec_detect=detect_breaker_block,
        extended_detect=_detect_breaker_block_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = ["_detect_breaker_block_extended", "detect_breaker_block", "detect_breaker_block_setup"]

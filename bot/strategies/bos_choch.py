"""bos_choch - canonical strategy detector."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, ClassVar, cast

from ..features.prepare import _swing_points
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.smc import latest_structure_break, swing_highs_lows
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ..setups.utils import coerce_int
from ._common import SpecHit, _latest_values, _row_volume_ratio, as_float, with_spec_columns

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.strategies.bos_choch")

__all__ = ["detect_bos_choch"]


def detect_bos_choch(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    max_age: int = 28,
    swing_length: int = 5,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    current = _latest_values(work)
    current_idx = int(work.item(-1, "_spec_idx"))
    current_close = as_float(current.get("close"))
    current_atr = as_float(current.get("spec_atr14"))
    if current_close <= 0.0 or current_atr <= 0.0:
        return None

    structure_zone = latest_structure_break(
        work,
        swing_length=max(2, int(swing_length)),
        prefer_kind="choch",
    )
    if structure_zone is None or structure_zone.kind not in {"bos", "choch"}:
        return None
    if structure_zone.level is None or structure_zone.broken_index is None:
        return None

    broken_index = max(0, min(int(structure_zone.broken_index), work.height - 1))
    age = current_idx - broken_index
    if age > max_age:
        return None

    break_level = float(structure_zone.level)
    direction = structure_zone.direction
    row = work.row(broken_index, named=True)
    atr = as_float(row.get("spec_atr14"), current_atr)
    close = as_float(row.get("close"))
    if min(close, break_level, atr) <= 0.0:
        return None

    if direction == "long":
        if current_close <= break_level:
            return None
        clarity = min(1.0, (close - break_level) / max(atr, 1e-8))
        return SpecHit(
            strategy="bos_choch",
            direction="long",
            entry=break_level,
            stop_basis=break_level - atr,
            atr=atr,
            timeframe=timeframe,
            reasons=(
                f"{structure_zone.kind}_break_above={break_level:.4f}",
                f"break_age={age}",
            ),
            structure_clarity=clarity,
            vol_ratio=_row_volume_ratio(row),
            rsi=as_float(row.get("rsi14"), 50.0),
            source_index=broken_index,
        )

    if current_close >= break_level:
        return None
    clarity = min(1.0, (break_level - close) / max(atr, 1e-8))
    return SpecHit(
        strategy="bos_choch",
        direction="short",
        entry=break_level,
        stop_basis=break_level + atr,
        atr=atr,
        timeframe=timeframe,
        reasons=(
            f"{structure_zone.kind}_break_below={break_level:.4f}",
            f"break_age={age}",
        ),
        structure_clarity=clarity,
        vol_ratio=_row_volume_ratio(row),
        rsi=as_float(row.get("rsi14"), 50.0),
        source_index=broken_index,
    )


_MIN_SWINGS = 6  # Need 3+ of each type for trend context


def _classify_swing_chain(
    sh_vals: list[float],
    sl_vals: list[float],
) -> str | None:
    """Build HH/HL/LH/LL chain from last N swings, return 'uptrend', 'downtrend', or None."""
    if len(sh_vals) < 3 or len(sl_vals) < 3:
        return None
    recent_highs = sh_vals[-3:]
    recent_lows = sl_vals[-3:]
    hh = recent_highs[-1] > recent_highs[-2] > recent_highs[-3]
    hl = recent_lows[-1] > recent_lows[-2] > recent_lows[-3]
    lh = recent_highs[-1] < recent_highs[-2] < recent_highs[-3]
    ll = recent_lows[-1] < recent_lows[-2] < recent_lows[-3]
    if hh and hl:
        return "uptrend"
    if lh and ll:
        return "downtrend"
    if hh and not lh:
        return "uptrend"
    if ll and not hl:
        return "downtrend"
    return None


def _select_external_stop_level(
    *,
    markers: list[object],
    levels: list[object],
    search_end: int,
    marker: float,
    price: float,
    above_price: bool,
) -> tuple[float | None, dict[str, object]]:
    """Select the latest external swing stop anchor and return reject diagnostics."""
    bounded_end = min(search_end, len(markers) - 1, len(levels) - 1)
    details: dict[str, object] = {
        "external_search_end": bounded_end,
        "external_marker": marker,
        "external_side_above_price": above_price,
    }
    if bounded_end < 0:
        details.update(
            external_marker_candidates=0,
            external_invalid_markers=0,
            external_invalid_levels=0,
            external_side_filtered=0,
        )
        return None, details

    marker_candidates = 0
    invalid_markers = 0
    invalid_levels = 0
    side_filtered = 0
    for idx in range(bounded_end, -1, -1):
        raw_marker = markers[idx]
        try:
            marker_value = float(cast("Any", raw_marker)) if raw_marker is not None else 0.0
        except TypeError, ValueError:
            invalid_markers += 1
            continue
        if raw_marker is None or marker_value != marker:
            continue
        marker_candidates += 1
        raw_level = levels[idx]
        if raw_level is None:
            invalid_levels += 1
            continue
        try:
            level = float(cast("Any", raw_level))
        except TypeError, ValueError:
            invalid_levels += 1
            continue
        if not math.isfinite(level) or level <= 0.0:
            invalid_levels += 1
            continue
        if above_price and level > price:
            details.update(
                external_marker_candidates=marker_candidates,
                external_invalid_markers=invalid_markers,
                external_invalid_levels=invalid_levels,
                external_side_filtered=side_filtered,
            )
            details["external_selected_index"] = idx
            details["external_selected_level"] = level
            return level, details
        if not above_price and level < price:
            details.update(
                external_marker_candidates=marker_candidates,
                external_invalid_markers=invalid_markers,
                external_invalid_levels=invalid_levels,
                external_side_filtered=side_filtered,
            )
            details["external_selected_index"] = idx
            details["external_selected_level"] = level
            return level, details
        side_filtered += 1
    details.update(
        external_marker_candidates=marker_candidates,
        external_invalid_markers=invalid_markers,
        external_invalid_levels=invalid_levels,
        external_side_filtered=side_filtered,
    )
    return None, details


def _prefix_stop_details(prefix: str, details: dict[str, object]) -> dict[str, object]:
    prefixed: dict[str, object] = {}
    for key, value in details.items():
        suffix = key.removeprefix("external_")
        prefixed[f"{prefix}_{suffix}"] = value
    return prefixed


def _select_stop_level_with_fallback(
    *,
    frame: pl.DataFrame,
    external_markers: list[object],
    external_levels: list[object],
    internal_markers: list[object],
    internal_levels: list[object],
    search_end: int,
    marker: float,
    price: float,
    break_level: float,
    atr: float,
    above_price: bool,
) -> tuple[float | None, str | None, dict[str, object]]:
    external_level, external_details = _select_external_stop_level(
        markers=external_markers,
        levels=external_levels,
        search_end=search_end,
        marker=marker,
        price=price,
        above_price=above_price,
    )
    details = _prefix_stop_details("external", external_details)
    if external_level is not None:
        details["stop_source"] = "external_swing"
        return external_level, "external_swing", details

    if atr > 0.0 and math.isfinite(atr):
        atr_level = break_level + 2.0 * atr if above_price else break_level - 2.0 * atr
        if atr_level > price if above_price else atr_level < price:
            details["stop_source"] = "atr_stop"
            details["fallback_used"] = "atr_2x_stop"
            details["atr_stop_level"] = atr_level
            details["atr_stop_atr"] = atr
            return atr_level, "atr_stop", details

    if frame.height >= 2:
        previous_level = float(frame.item(-2, "high" if above_price else "low"))
        if (
            math.isfinite(previous_level)
            and previous_level > 0.0
            and (previous_level > price if above_price else previous_level < price)
        ):
            details["stop_source"] = "previous_candle"
            details["fallback_used"] = "previous_candle_stop"
            details["previous_candle_stop_level"] = previous_level
            return previous_level, "previous_candle", details

    internal_level, internal_details = _select_external_stop_level(
        markers=internal_markers,
        levels=internal_levels,
        search_end=search_end,
        marker=marker,
        price=price,
        above_price=above_price,
    )
    details.update(_prefix_stop_details("internal", internal_details))
    if internal_level is not None:
        details["stop_source"] = "internal_swing"
        details["fallback_used"] = "internal_swing_stop"
        return internal_level, "internal_swing", details

    details["stop_source"] = "not_available"
    details["fallback_used"] = "not_used"
    return None, None, details


def _spec_detect_kwargs(effective: dict[str, float]) -> dict[str, object]:
    swing_lookback = coerce_int(
        effective.get("swing_lookback", effective.get("bos_lookback", 6)),
        6,
    )
    return {
        "max_age": coerce_int(effective.get("max_break_age_bars", 20), 20),
        "swing_length": max(2, swing_lookback),
    }


def _detect_bos_choch_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective

    configured_swing_lookback = coerce_int(
        dynamic_params.get("swing_lookback"), int(defaults["swing_lookback"])
    )
    bos_lookback = coerce_int(dynamic_params.get("bos_lookback"), configured_swing_lookback)
    choch_lookback = coerce_int(dynamic_params.get("choch_lookback"), configured_swing_lookback)
    swing_lookback = max(2, bos_lookback, choch_lookback, configured_swing_lookback)
    external_swing_lookback = max(
        swing_lookback + 1,
        coerce_int(
            dynamic_params.get("external_swing_lookback"),
            int(defaults["external_swing_lookback"]),
        ),
    )
    sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]))
    min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
    base_score = float(dynamic_params.get("base_score", defaults["base_score"]))
    breakout_threshold_atr = float(
        dynamic_params.get("breakout_threshold_atr", defaults["breakout_threshold_atr"])
    )
    max_break_age_bars = coerce_int(
        dynamic_params.get("max_break_age_bars"), int(defaults["max_break_age_bars"])
    )
    max_retest_age_bars = coerce_int(
        dynamic_params.get("max_retest_age_bars"), int(defaults["max_retest_age_bars"])
    )
    retest_atr_mult = float(dynamic_params.get("retest_atr_mult", defaults["retest_atr_mult"]))
    min_volume_ratio = float(dynamic_params.get("min_volume_ratio", defaults["min_volume_ratio"]))

    w = prepared.work_15m
    min_height = external_swing_lookback * 2 + 1
    if w.height < min_height:
        _reject(
            prepared,
            setup_id,
            "insufficient_height",
            actual=w.height,
            required=min_height,
            external_swing_lookback=external_swing_lookback,
        )
        return None

    atr = float(w.item(-1, "atr14") or 0.0)
    if atr <= 0 or math.isnan(atr):
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    last_bar_is_closed = True
    if "is_closed" in w.columns:
        last_bar_is_closed = bool(w["is_closed"].item(-1))
    if not last_bar_is_closed:
        unconfirmed_zone = latest_structure_break(
            w,
            swing_length=swing_lookback,
            prefer_kind="choch",
        )
        if (
            unconfirmed_zone is not None
            and unconfirmed_zone.kind in {"bos", "choch"}
            and int(unconfirmed_zone.broken_index or 0) >= w.height - 1
        ):
            _reject(prepared, setup_id, "structure_break_on_unconfirmed_bar")
            return None

    scan = w if last_bar_is_closed else w.head(w.height - 1)
    if scan.height < min_height:
        _reject(
            prepared,
            setup_id,
            "insufficient_confirmed_15m_bars",
            bars=scan.height,
            required=min_height,
        )
        return None

    sh_mask, sl_mask = _swing_points(scan, n=swing_lookback)
    sh_prices = scan.filter(sh_mask)["high"]
    sl_prices = scan.filter(sl_mask)["low"]

    # Need at least 3 of each to determine prior trend + break
    min_swings = max(
        3,
        coerce_int(dynamic_params.get("min_swings"), int(defaults["min_swings"])),
    )
    if sh_prices.len() < min_swings or sl_prices.len() < min_swings:
        _reject(
            prepared,
            setup_id,
            "insufficient_swing_points",
            swing_highs=sh_prices.len(),
            swing_lows=sl_prices.len(),
            min_swings=min_swings,
        )
        return None

    sh_vals = sh_prices.to_numpy()
    sl_vals = sl_prices.to_numpy()

    structure_zone = latest_structure_break(
        scan,
        swing_length=swing_lookback,
        prefer_kind="choch",
    )
    if structure_zone is None:
        _reject(prepared, setup_id, "no_bos_choch_detected")
        return None
    if structure_zone.kind not in {"bos", "choch"}:
        _reject(prepared, setup_id, "invalid_structure_break_kind", kind=structure_zone.kind)
        return None
    break_kind = str(structure_zone.kind)
    direction = structure_zone.direction
    if structure_zone.level is None or structure_zone.broken_index is None:
        _reject(
            prepared,
            setup_id,
            "invalid_bos_choch_zone",
            level=structure_zone.level,
            broken_index=structure_zone.broken_index,
        )
        return None
    broken_index = int(structure_zone.broken_index)
    broken_index = max(0, min(broken_index, scan.height - 1))
    break_age = scan.height - 1 - broken_index
    break_level = float(structure_zone.level)
    retest_active = break_age <= max_retest_age_bars and abs(price - break_level) <= (
        atr * retest_atr_mult
    )
    if break_age > max_break_age_bars and not retest_active:
        _reject(
            prepared,
            setup_id,
            "structure_break_too_old",
            break_age=break_age,
            max_break_age_bars=max_break_age_bars,
            max_retest_age_bars=max_retest_age_bars,
            distance_atr=abs(price - break_level) / atr,
            kind=break_kind,
        )
        return None
    raw_break_close = scan.item(broken_index, "close")
    if raw_break_close is None:
        _reject(prepared, setup_id, "break_close_missing", broken_index=broken_index)
        return None
    break_close = float(raw_break_close)
    break_distance = break_close - break_level if direction == "long" else break_level - break_close
    min_break_distance = atr * breakout_threshold_atr
    if break_distance < min_break_distance:
        _reject(
            prepared,
            setup_id,
            "structure_break_too_weak",
            break_distance=break_distance,
            min_break_distance=min_break_distance,
            breakout_threshold_atr=breakout_threshold_atr,
            kind=break_kind,
        )
        return None
    vol_ratio = float(scan.item(broken_index, "volume_ratio20") or 1.0)
    if vol_ratio < min_volume_ratio:
        _reject(
            prepared,
            setup_id,
            "structure_break_volume_too_low",
            vol_ratio=vol_ratio,
            min_volume_ratio=min_volume_ratio,
            kind=break_kind,
        )
        return None

    chain_trend = _classify_swing_chain(sh_vals.tolist(), sl_vals.tolist())
    chain_ok = chain_trend is None or (
        (direction == "long" and chain_trend == "uptrend")
        or (direction == "short" and chain_trend == "downtrend")
    )
    if not chain_ok:
        _reject(
            prepared,
            setup_id,
            "swing_chain_opposes_break",
            direction=direction,
            chain_trend=chain_trend,
            swing_highs=sh_vals.tolist()[-3:],
            swing_lows=sl_vals.tolist()[-3:],
            break_kind=break_kind,
        )
        return None

    stop_price = None
    pivot_level = None
    entry_price = break_level

    external_swings = swing_highs_lows(
        scan,
        swing_length=external_swing_lookback,
        mode="live_safe",
    )
    external_markers = external_swings["HighLow"].to_list()
    external_levels = external_swings["Level"].to_list()
    internal_swings = swing_highs_lows(
        scan,
        swing_length=swing_lookback,
        mode="live_safe",
    )
    internal_markers = internal_swings["HighLow"].to_list()
    internal_levels = internal_swings["Level"].to_list()
    external_search_end = min(
        int(structure_zone.broken_index),
        len(external_markers) - 1,
        len(external_levels) - 1,
        len(internal_markers) - 1,
        len(internal_levels) - 1,
    )

    # --- Compute structural SL/TP ---
    if direction == "long":
        pivot_level, stop_source, stop_details = _select_stop_level_with_fallback(
            frame=scan,
            external_markers=external_markers,
            external_levels=external_levels,
            internal_markers=internal_markers,
            internal_levels=internal_levels,
            search_end=external_search_end,
            marker=-1.0,
            price=entry_price,
            break_level=entry_price,
            atr=atr,
            above_price=False,
        )
        if pivot_level is None:
            _reject(
                prepared,
                setup_id,
                "swing_stop_missing_long",
                external_swing_lookback=external_swing_lookback,
                swing_lookback=swing_lookback,
                **cast("Any", stop_details),
            )
            return None
        stop_price = (
            pivot_level
            if stop_source in {"atr_stop", "previous_candle"}
            else pivot_level - sl_buffer_atr * atr
        )
        risk = entry_price - stop_price
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_long",
                stop=stop_price,
                price=entry_price,
            )
            return None
        # TP1: last swing high before the structural break
        tp1 = float(sh_vals[-2]) if sh_vals[-2] > entry_price else None
        # TP2: 4h swing target
        w4h = prepared.work_4h
        tp2 = None
        if w4h is not None and w4h.height > 5:
            sh4_mask, _ = _swing_points(w4h, n=2)
            sh4_prices = w4h.filter(sh4_mask)["high"]
            tp2_cands = sh4_prices.filter(sh4_prices > entry_price)
            tp2 = float(tp2_cands[0]) if tp2_cands.len() > 0 else None
    else:
        pivot_level, stop_source, stop_details = _select_stop_level_with_fallback(
            frame=scan,
            external_markers=external_markers,
            external_levels=external_levels,
            internal_markers=internal_markers,
            internal_levels=internal_levels,
            search_end=external_search_end,
            marker=1.0,
            price=entry_price,
            break_level=entry_price,
            atr=atr,
            above_price=True,
        )
        if pivot_level is None:
            _reject(
                prepared,
                setup_id,
                "swing_stop_missing_short",
                external_swing_lookback=external_swing_lookback,
                swing_lookback=swing_lookback,
                **cast("Any", stop_details),
            )
            return None
        stop_price = (
            pivot_level
            if stop_source in {"atr_stop", "previous_candle"}
            else pivot_level + sl_buffer_atr * atr
        )
        risk = stop_price - entry_price
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_short",
                stop=stop_price,
                price=entry_price,
            )
            return None
        # TP1: last swing low before the structural break
        tp1 = float(sl_vals[-2]) if sl_vals[-2] < entry_price else None
        # TP2: 4h swing target
        w4h = prepared.work_4h
        tp2 = None
        if w4h is not None and w4h.height > 5:
            _, sl4_mask = _swing_points(w4h, n=2)
            sl4_prices = w4h.filter(sl4_mask)["low"]
            tp2_cands = sl4_prices.filter(sl4_prices < entry_price)
            tp2 = float(tp2_cands[-1]) if tp2_cands.len() > 0 else None

    fallback_note = None
    if tp1 is None or abs(tp1 - entry_price) < risk * min_rr:
        tp1 = entry_price + risk * min_rr if direction == "long" else entry_price - risk * min_rr
        fallback_note = f"tp1_rr_fallback_{min_rr:.2f}"
    if direction == "long":
        if tp2 is None or tp2 <= tp1:
            tp2 = entry_price + risk * max(2.0, min_rr + 0.35)
    else:
        if tp2 is None or tp2 >= tp1:
            tp2 = entry_price - risk * max(2.0, min_rr + 0.35)

    rsi = float(w.item(-1, "rsi14") or 50.0)
    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
    )
    if break_kind == "bos":
        score *= float(dynamic_params.get("bos_score_multiplier", 0.94))

    reasons = [
        f"{break_kind.upper()} {direction}: structure level={structure_zone.level:.4f}",
        f"break_close={break_close:.4f} break_age={break_age}",
        f"price={price:.4f} limit_entry={entry_price:.4f}",
        f"break_distance_atr={break_distance / atr:.2f}",
        f"retest_active={retest_active}",
        f"vol_ratio={vol_ratio:.2f}",
        f"{stop_source}_sl={pivot_level:.4f}",
        f"sh[-3]={sh_vals[-3]:.4f} sh[-2]={sh_vals[-2]:.4f} sh[-1]={sh_vals[-1]:.4f}",
        f"sl[-3]={sl_vals[-3]:.4f} sl[-2]={sl_vals[-2]:.4f} sl[-1]={sl_vals[-1]:.4f}",
    ]
    if fallback_note:
        reasons.append(fallback_note)

    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe="15m",
        reasons=reasons,
        strategy_family=family,
        stop=stop_price,
        tp1=tp1,
        tp2=tp2,
        price_anchor=entry_price,
        atr=atr,
    )


def detect_bos_choch_setup(
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
        spec_detect=detect_bos_choch,
        extended_detect=_detect_bos_choch_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = ["_detect_bos_choch_extended", "detect_bos_choch", "detect_bos_choch_setup"]


class BOSCHOCHSetup(SpecDetectorSetup):
    setup_id = "bos_choch"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.62,
        "swing_lookback": 6,
        "external_swing_lookback": 20,
        "bos_lookback": 6,
        "choch_lookback": 6,
        "sl_buffer_atr": 0.5,
        "breakout_threshold_atr": 0.4,
        "max_break_age_bars": 6,
        "max_retest_age_bars": 16,
        "retest_atr_mult": 1.25,
        "min_volume_ratio": 1.05,
        "bias_mismatch_penalty": 0.75,
        "min_rr": 1.9,
        "min_swings": 3,
    }

    detect_setup = detect_bos_choch_setup

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        """Detect BOS/CHoCH signal for given symbol."""
        try:
            return super().detect(prepared, settings)
        except (ValueError, KeyError, IndexError) as e:
            LOG.exception("%s bos_choch: detection error", prepared.symbol)
            _reject(
                prepared,
                self.setup_id,
                "runtime.unexpected_exception",
                stage="runtime",
                exception_type=type(e).__name__,
            )
            return None


__all__ = ["BOSCHOCHSetup"]

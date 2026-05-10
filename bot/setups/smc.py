from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import numpy as np
from numpy.typing import NDArray
import polars as pl

from ..features import _swing_points

SMCMode = Literal["offline_parity", "live_safe"]
ZoneState = Literal["fresh", "mitigated", "invalidated"]


@dataclass(slots=True)
class SMCZone:
    kind: str
    direction: str
    top: float
    bottom: float
    created_index: int
    state: ZoneState
    midpoint: float
    width: float
    level: float | None = None
    mitigation_index: int | None = None
    invalidation_index: int | None = None
    broken_index: int | None = None
    end_index: int | None = None
    sweep_index: int | None = None
    metadata: dict[str, float | int | str | None] = field(default_factory=dict)


def _normalize_ohlcv(
    frame: pl.DataFrame, *, require_volume: bool = False
) -> pl.DataFrame:
    rename_map = {
        column: column.lower() for column in frame.columns if column.lower() != column
    }
    normalized = frame.rename(rename_map) if rename_map else frame
    required = {"open", "high", "low", "close"}
    if require_volume:
        required.add("volume")
    missing = sorted(required.difference(normalized.columns))
    if missing:
        raise LookupError(f"missing ohlcv columns: {', '.join(missing)}")
    return normalized


def _series_to_float_array(series: pl.Series) -> NDArray[np.float64]:
    return series.cast(pl.Float64, strict=False).to_numpy()


def _collapse_swing_markers(
    markers: NDArray[np.float64],
    highs: NDArray[np.float64],
    lows: NDArray[np.float64],
    *,
    keep_edge_adjustments: bool,
) -> np.ndarray:
    markers = markers.copy()

    while True:
        positions = np.flatnonzero(~np.isnan(markers))
        if positions.size < 2:
            break

        current = markers[positions[:-1]]
        nxt = markers[positions[1:]]
        highs_now = highs[positions[:-1]]
        lows_now = lows[positions[:-1]]
        highs_next = highs[positions[1:]]
        lows_next = lows[positions[1:]]

        remove = np.zeros(positions.size, dtype=bool)
        consecutive_highs = (current == 1.0) & (nxt == 1.0)
        remove[:-1] |= consecutive_highs & (highs_now < highs_next)
        remove[1:] |= consecutive_highs & (highs_now >= highs_next)

        consecutive_lows = (current == -1.0) & (nxt == -1.0)
        remove[:-1] |= consecutive_lows & (lows_now > lows_next)
        remove[1:] |= consecutive_lows & (lows_now <= lows_next)

        if not remove.any():
            break

        markers[positions[remove]] = np.nan

    if keep_edge_adjustments:
        pass

    return markers


def fvg(
    frame: pl.DataFrame,
    *,
    join_consecutive: bool = False,
) -> pl.DataFrame:
    ohlc = _normalize_ohlcv(frame)
    open_ = _series_to_float_array(ohlc["open"])
    high = _series_to_float_array(ohlc["high"])
    low = _series_to_float_array(ohlc["low"])
    close = _series_to_float_array(ohlc["close"])
    length = ohlc.height

    gap = np.full(length, np.nan, dtype=np.float64)
    top = np.full(length, np.nan, dtype=np.float64)
    bottom = np.full(length, np.nan, dtype=np.float64)

    for i in range(1, max(length - 1, 1)):
        bullish = high[i - 1] < low[i + 1] and close[i] > open_[i]
        bearish = low[i - 1] > high[i + 1] and close[i] < open_[i]
        if bullish:
            gap[i] = 1.0
            top[i] = low[i + 1]
            bottom[i] = high[i - 1]
        elif bearish:
            gap[i] = -1.0
            top[i] = low[i - 1]
            bottom[i] = high[i + 1]

    if join_consecutive:
        for i in range(max(length - 1, 0)):
            if np.isnan(gap[i]) or np.isnan(gap[i + 1]) or gap[i] != gap[i + 1]:
                continue
            top[i + 1] = max(top[i], top[i + 1])
            bottom[i + 1] = min(bottom[i], bottom[i + 1])
            gap[i] = np.nan
            top[i] = np.nan
            bottom[i] = np.nan

    mitigated_index = np.full(length, np.nan, dtype=np.float64)
    for i in cast(Any, np.flatnonzero(~np.isnan(gap))):
        if gap[i] == 1.0:
            mask = close[i + 2 :] <= top[i]
        else:
            mask = close[i + 2 :] >= bottom[i]
        if np.any(mask):
            mitigated_index[i] = float(np.argmax(mask) + i + 2)
    mitigated_index = np.where(np.isnan(gap), np.nan, mitigated_index)

    return pl.DataFrame(
        {
            "FVG": gap,
            "Top": top,
            "Bottom": bottom,
            "MitigatedIndex": mitigated_index,
        }
    )


def swing_highs_lows(
    frame: pl.DataFrame,
    *,
    swing_length: int = 50,
    mode: SMCMode = "live_safe",
    include_unconfirmed_tail: bool = False,
) -> pl.DataFrame:
    ohlc = _normalize_ohlcv(frame)
    highs = _series_to_float_array(ohlc["high"])
    lows = _series_to_float_array(ohlc["low"])

    if mode == "live_safe":
        swing_high, swing_low = _swing_points(
            ohlc,
            n=max(1, int(swing_length)),
            include_unconfirmed_tail=include_unconfirmed_tail,
        )
        markers = np.where(
            swing_high.to_numpy(),
            1.0,
            np.where(swing_low.to_numpy(), -1.0, np.nan),
        ).astype(np.float64)
        markers = _collapse_swing_markers(
            markers,
            highs,
            lows,
            keep_edge_adjustments=False,
        )
    else:
        half = max(1, int(swing_length))
        window = max(2, half * 2)
        shifted_high_max = _series_to_float_array(
            ohlc["high"].shift(-half).rolling_max(window_size=window)
        )
        shifted_low_min = _series_to_float_array(
            ohlc["low"].shift(-half).rolling_min(window_size=window)
        )
        markers = np.where(
            highs == shifted_high_max,
            1.0,
            np.where(lows == shifted_low_min, -1.0, np.nan),
        ).astype(np.float64)
        markers = _collapse_swing_markers(
            markers,
            highs,
            lows,
            keep_edge_adjustments=True,
        )

    levels = np.where(
        ~np.isnan(markers),
        np.where(markers == 1.0, highs, lows),
        np.nan,
    )
    return pl.DataFrame({"HighLow": markers, "Level": levels})


def bos_choch(
    frame: pl.DataFrame,
    swings: pl.DataFrame,
    *,
    close_break: bool = True,
) -> pl.DataFrame:
    ohlc = _normalize_ohlcv(frame)
    swing_frame = swings.clone()

    high_low = _series_to_float_array(swing_frame["HighLow"])
    levels_in = _series_to_float_array(swing_frame["Level"])
    breaks_source = _series_to_float_array(ohlc["close" if close_break else "high"])
    low_breaks_source = _series_to_float_array(ohlc["close" if close_break else "low"])
    length = ohlc.height

    level_order: list[float] = []
    highs_lows_order: list[float] = []
    last_positions: list[int] = []

    bos = np.zeros(length, dtype=np.int32)
    choch = np.zeros(length, dtype=np.int32)
    levels_out = np.zeros(length, dtype=np.float32)

    for i in range(length):
        if np.isnan(high_low[i]):
            continue
        level_order.append(float(levels_in[i]))
        highs_lows_order.append(float(high_low[i]))
        if len(level_order) >= 4 and len(last_positions) >= 3:
            anchor = last_positions[-2]
            recent_hl = highs_lows_order[-4:]
            recent_levels = level_order[-4:]

            bullish_sequence = recent_hl == [-1.0, 1.0, -1.0, 1.0]
            bearish_sequence = recent_hl == [1.0, -1.0, 1.0, -1.0]
            bullish_bos = bullish_sequence and (
                recent_levels[0]
                < recent_levels[2]
                < recent_levels[1]
                < recent_levels[3]
                or (
                    recent_levels[2] > recent_levels[0]
                    and recent_levels[3] > recent_levels[1]
                )
            )
            bearish_bos = bearish_sequence and (
                recent_levels[0]
                > recent_levels[2]
                > recent_levels[1]
                > recent_levels[3]
                or (
                    recent_levels[2] < recent_levels[0]
                    and recent_levels[3] < recent_levels[1]
                )
            )
            bullish_choch = bullish_sequence and (
                recent_levels[3]
                > recent_levels[1]
                > recent_levels[0]
                > recent_levels[2]
                or (
                    recent_levels[3] > recent_levels[1]
                    and recent_levels[2] < recent_levels[0]
                )
            )
            bearish_choch = bearish_sequence and (
                recent_levels[3]
                < recent_levels[1]
                < recent_levels[0]
                < recent_levels[2]
                or (
                    recent_levels[3] < recent_levels[1]
                    and recent_levels[2] > recent_levels[0]
                )
            )

            bos[anchor] = 1 if bullish_bos else 0
            levels_out[anchor] = recent_levels[1] if bos[anchor] != 0 else 0.0

            bos[anchor] = -1 if bearish_bos else bos[anchor]
            levels_out[anchor] = recent_levels[1] if bos[anchor] != 0 else 0.0

            choch[anchor] = 1 if bullish_choch else 0
            levels_out[anchor] = (
                recent_levels[1] if choch[anchor] != 0 else levels_out[anchor]
            )

            choch[anchor] = -1 if bearish_choch else choch[anchor]
            levels_out[anchor] = (
                recent_levels[1] if choch[anchor] != 0 else levels_out[anchor]
            )

        last_positions.append(i)

    broken = np.zeros(length, dtype=np.int32)
    active_indices = np.flatnonzero((bos != 0) | (choch != 0))
    for i in cast(Any, active_indices):
        if bos[i] == 1 or choch[i] == 1:
            mask = breaks_source[i + 2 :] > levels_out[i]
        else:
            mask = low_breaks_source[i + 2 :] < levels_out[i]
        if np.any(mask):
            j = int(np.argmax(mask) + i + 2)
            broken[i] = j

    bos_out = np.where(bos != 0, bos.astype(np.float64), np.nan)
    choch_out = np.where(choch != 0, choch.astype(np.float64), np.nan)
    level_out = np.where(levels_out != 0.0, levels_out, np.nan)
    broken_out = np.where(broken != 0, broken.astype(np.float64), np.nan)
    return pl.DataFrame(
        {
            "BOS": bos_out,
            "CHOCH": choch_out,
            "Level": level_out,
            "BrokenIndex": broken_out,
        }
    )


def order_blocks(
    frame: pl.DataFrame,
    swings: pl.DataFrame,
    *,
    close_mitigation: bool = False,
) -> pl.DataFrame:
    ohlcv = _normalize_ohlcv(frame, require_volume=True)
    length = ohlcv.height
    open_ = _series_to_float_array(ohlcv["open"])
    high = _series_to_float_array(ohlcv["high"])
    low = _series_to_float_array(ohlcv["low"])
    close = _series_to_float_array(ohlcv["close"])
    volume = _series_to_float_array(ohlcv["volume"])
    swing_hl = _series_to_float_array(swings["HighLow"])

    crossed = np.full(length, False, dtype=bool)
    ob = np.zeros(length, dtype=np.int32)
    top_arr = np.zeros(length, dtype=np.float64)
    bottom_arr = np.zeros(length, dtype=np.float64)
    ob_volume = np.zeros(length, dtype=np.float64)
    low_volume = np.zeros(length, dtype=np.float64)
    high_volume = np.zeros(length, dtype=np.float64)
    percentage = np.zeros(length, dtype=np.float64)
    mitigated_index = np.full(length, np.nan, dtype=np.float64)
    breaker = np.full(length, False, dtype=bool)

    swing_high_indices = np.flatnonzero(swing_hl == 1.0)
    swing_low_indices = np.flatnonzero(swing_hl == -1.0)

    active_bullish: list[int] = []
    for close_index in range(length):
        for idx in active_bullish.copy():
            if breaker[idx]:
                if high[close_index] > top_arr[idx]:
                    ob[idx] = 0
                    top_arr[idx] = 0.0
                    bottom_arr[idx] = 0.0
                    ob_volume[idx] = 0.0
                    low_volume[idx] = 0.0
                    high_volume[idx] = 0.0
                    mitigated_index[idx] = np.nan
                    percentage[idx] = 0.0
                    active_bullish.remove(idx)
            elif (not close_mitigation and low[close_index] < bottom_arr[idx]) or (
                close_mitigation
                and min(open_[close_index], close[close_index]) < bottom_arr[idx]
            ):
                breaker[idx] = True
                mitigated_index[idx] = close_index - 1

        pos = int(np.searchsorted(swing_high_indices, close_index))
        last_top_index = int(swing_high_indices[pos - 1]) if pos > 0 else None
        if last_top_index is None:
            continue
        if close[close_index] <= high[last_top_index] or crossed[last_top_index]:
            continue

        crossed[last_top_index] = True
        default_index = max(close_index - 1, 0)
        ob_bottom = high[default_index]
        ob_top = low[default_index]
        ob_index = default_index

        if close_index - last_top_index > 1:
            start = last_top_index + 1
            end = close_index
            if end > start:
                segment = low[start:end]
                min_value = float(segment.min())
                candidates = np.nonzero(segment == min_value)[0]
                if candidates.size:
                    candidate_index = int(start + candidates[-1])
                    ob_bottom = low[candidate_index]
                    ob_top = high[candidate_index]
                    ob_index = candidate_index

        ob[ob_index] = 1
        top_arr[ob_index] = ob_top
        bottom_arr[ob_index] = ob_bottom
        vol_cur = volume[ob_index]
        vol_prev1 = volume[ob_index - 1] if ob_index >= 1 else 0.0
        vol_prev2 = volume[ob_index - 2] if ob_index >= 2 else 0.0
        ob_volume[ob_index] = vol_cur + vol_prev1 + vol_prev2
        low_volume[ob_index] = vol_prev2
        high_volume[ob_index] = vol_cur + vol_prev1
        max_vol = max(high_volume[ob_index], low_volume[ob_index])
        percentage[ob_index] = (
            (min(high_volume[ob_index], low_volume[ob_index]) / max_vol) * 100.0
            if max_vol != 0.0
            else 100.0
        )
        active_bullish.append(ob_index)

    active_bearish: list[int] = []
    for close_index in range(length):
        for idx in active_bearish.copy():
            if breaker[idx]:
                if low[close_index] < bottom_arr[idx]:
                    ob[idx] = 0
                    top_arr[idx] = 0.0
                    bottom_arr[idx] = 0.0
                    ob_volume[idx] = 0.0
                    low_volume[idx] = 0.0
                    high_volume[idx] = 0.0
                    mitigated_index[idx] = np.nan
                    percentage[idx] = 0.0
                    active_bearish.remove(idx)
            elif (not close_mitigation and high[close_index] > top_arr[idx]) or (
                close_mitigation
                and max(open_[close_index], close[close_index]) > top_arr[idx]
            ):
                breaker[idx] = True
                mitigated_index[idx] = close_index

        pos = int(np.searchsorted(swing_low_indices, close_index))
        last_bottom_index = int(swing_low_indices[pos - 1]) if pos > 0 else None
        if last_bottom_index is None:
            continue
        if close[close_index] >= low[last_bottom_index] or crossed[last_bottom_index]:
            continue

        crossed[last_bottom_index] = True
        default_index = max(close_index - 1, 0)
        ob_top = high[default_index]
        ob_bottom = low[default_index]
        ob_index = default_index

        if close_index - last_bottom_index > 1:
            start = last_bottom_index + 1
            end = close_index
            if end > start:
                segment = high[start:end]
                max_value = float(segment.max())
                candidates = np.nonzero(segment == max_value)[0]
                if candidates.size:
                    candidate_index = int(start + candidates[-1])
                    ob_top = high[candidate_index]
                    ob_bottom = low[candidate_index]
                    ob_index = candidate_index

        ob[ob_index] = -1
        top_arr[ob_index] = ob_top
        bottom_arr[ob_index] = ob_bottom
        vol_cur = volume[ob_index]
        vol_prev1 = volume[ob_index - 1] if ob_index >= 1 else 0.0
        vol_prev2 = volume[ob_index - 2] if ob_index >= 2 else 0.0
        ob_volume[ob_index] = vol_cur + vol_prev1 + vol_prev2
        low_volume[ob_index] = vol_cur + vol_prev1
        high_volume[ob_index] = vol_prev2
        max_vol = max(high_volume[ob_index], low_volume[ob_index])
        percentage[ob_index] = (
            (min(high_volume[ob_index], low_volume[ob_index]) / max_vol) * 100.0
            if max_vol != 0.0
            else 100.0
        )
        active_bearish.append(ob_index)

    ob_out = np.where(ob != 0, ob.astype(np.float64), np.nan)
    top_out = np.where(~np.isnan(ob_out), top_arr, np.nan)
    bottom_out = np.where(~np.isnan(ob_out), bottom_arr, np.nan)
    volume_out = np.where(~np.isnan(ob_out), ob_volume, np.nan)
    mitigated_out = np.where(~np.isnan(ob_out), mitigated_index, np.nan)
    percentage_out = np.where(~np.isnan(ob_out), percentage, np.nan)

    return pl.DataFrame(
        {
            "OB": ob_out,
            "Top": top_out,
            "Bottom": bottom_out,
            "OBVolume": volume_out,
            "MitigatedIndex": mitigated_out,
            "Percentage": percentage_out,
        }
    )


def liquidity_pools(
    frame: pl.DataFrame,
    swings: pl.DataFrame,
    *,
    range_percent: float = 0.01,
) -> pl.DataFrame:
    ohlc = _normalize_ohlcv(frame)
    high = _series_to_float_array(ohlc["high"])
    low = _series_to_float_array(ohlc["low"])
    close = _series_to_float_array(ohlc["close"])
    swing_high_low = _series_to_float_array(swings["HighLow"])
    swing_level = _series_to_float_array(swings["Level"])
    length = ohlc.height

    prev_close = np.concatenate(([close[0]], close[:-1])) if length else close
    true_range = np.maximum.reduce(
        (
            np.abs(high - low),
            np.abs(high - prev_close),
            np.abs(low - prev_close),
        )
    )
    atr_window = true_range[-min(14, length) :] if length else true_range
    atr = float(np.nanmean(atr_window)) if atr_window.size else 0.0
    median_price = float(np.nanmedian(close)) if length else 0.0
    pip_range = atr * float(range_percent) * 2.0
    if pip_range <= 0.0:
        pip_range = abs(median_price) * float(range_percent)
    sweep_buffer = max(pip_range * 0.10, abs(median_price) * 1e-5)

    shl_hl = swing_high_low.copy()
    shl_level = swing_level.copy()

    liquidity = np.full(length, np.nan, dtype=np.float64)
    liquidity_level = np.full(length, np.nan, dtype=np.float64)
    liquidity_end = np.full(length, np.nan, dtype=np.float64)
    liquidity_swept = np.full(length, np.nan, dtype=np.float64)

    bull_indices = np.nonzero(shl_hl == 1.0)[0]
    for i in bull_indices:
        if shl_hl[i] != 1.0:
            continue
        high_level = shl_level[i]
        range_low = high_level - pip_range
        range_high = high_level + pip_range
        group_levels = [float(high_level)]
        group_end = int(i)

        start = i + 1
        if start < length:
            cond = high[start:] >= (range_high - sweep_buffer)
            swept = int(start + np.argmax(cond)) if np.any(cond) else None
        else:
            swept = None

        for j in bull_indices:
            if j <= i:
                continue
            if swept is not None and j >= swept:
                break
            if shl_hl[j] == 1.0 and range_low <= shl_level[j] <= range_high:
                group_levels.append(float(shl_level[j]))
                group_end = int(j)
                shl_hl[j] = 0.0

        if len(group_levels) > 1:
            liquidity[i] = 1.0
            liquidity_level[i] = sum(group_levels) / len(group_levels)
            liquidity_end[i] = float(group_end)
            liquidity_swept[i] = float(swept) if swept is not None else np.nan

    bear_indices = np.nonzero(shl_hl == -1.0)[0]
    for i in bear_indices:
        if shl_hl[i] != -1.0:
            continue
        low_level = shl_level[i]
        range_low = low_level - pip_range
        range_high = low_level + pip_range
        group_levels = [float(low_level)]
        group_end = int(i)

        start = i + 1
        if start < length:
            cond = low[start:] <= (range_low + sweep_buffer)
            swept = int(start + np.argmax(cond)) if np.any(cond) else None
        else:
            swept = None

        for j in bear_indices:
            if j <= i:
                continue
            if swept is not None and j >= swept:
                break
            if shl_hl[j] == -1.0 and range_low <= shl_level[j] <= range_high:
                group_levels.append(float(shl_level[j]))
                group_end = int(j)
                shl_hl[j] = 0.0

        if len(group_levels) > 1:
            liquidity[i] = -1.0
            liquidity_level[i] = sum(group_levels) / len(group_levels)
            liquidity_end[i] = float(group_end)
            liquidity_swept[i] = float(swept) if swept is not None else np.nan

    return pl.DataFrame(
        {
            "Liquidity": liquidity,
            "Level": liquidity_level,
            "End": liquidity_end,
            "Swept": liquidity_swept,
        }
    )


def _optional_index(value: float | None) -> int | None:
    """Return ``None`` for null/NaN index values, otherwise an ``int``."""
    if _is_missing(value):
        return None
    return int(cast(Any, value))


def _is_missing(value: object) -> bool:
    """Treat Python nulls and floating NaN values as missing SMC cells."""
    if value is None:
        return True
    try:
        return bool(np.isnan(cast(Any, value)))
    except (TypeError, ValueError):
        return False


def _zone_state(
    mitigation_index: int | None,
    invalidation_index: int | None,
) -> ZoneState:
    if invalidation_index is not None:
        return "invalidated"
    if mitigation_index is not None:
        return "mitigated"
    return "fresh"


def _fvg_invalidation_index(
    frame: pl.DataFrame,
    *,
    direction: str,
    top: float,
    bottom: float,
    created_index: int,
) -> int | None:
    ohlc = _normalize_ohlcv(frame)
    high = _series_to_float_array(ohlc["high"])
    low = _series_to_float_array(ohlc["low"])
    start = created_index + 2
    if start >= ohlc.height:
        return None
    if direction == "long":
        mask = low[start:] <= bottom
    else:
        mask = high[start:] >= top
    if not np.any(mask):
        return None
    return int(np.argmax(mask) + start)


def _fvg_fill_metrics(
    frame: pl.DataFrame,
    *,
    direction: str,
    top: float,
    bottom: float,
    created_index: int,
) -> dict[str, float | int]:
    """Measure FVG fill percentage and age-based confidence decay."""
    width = max(abs(float(top) - float(bottom)), 0.0)
    age_bars = max(int(frame.height) - int(created_index) - 1, 0)
    if width <= 0.0 or created_index >= frame.height - 1:
        fill_pct = 0.0
    elif direction == "long":
        after_lows = frame["low"].slice(created_index + 1)
        lowest = float(cast(Any, after_lows.min())) if after_lows.len() else float(top)
        fill_pct = (float(top) - lowest) / width
    else:
        after_highs = frame["high"].slice(created_index + 1)
        highest = float(cast(Any, after_highs.max())) if after_highs.len() else float(bottom)
        fill_pct = (highest - float(bottom)) / width
    fill_pct = max(0.0, min(1.0, fill_pct))
    fill_penalty = 0.75 if fill_pct > 0.50 else 1.0
    age_decay = math.exp(-max(age_bars - 40, 0) / 60.0)
    return {
        "fvg_fill_pct": round(fill_pct, 6),
        "age_bars": age_bars,
        "confidence_multiplier": round(max(0.0, min(1.0, fill_penalty * age_decay)), 6),
    }


def _order_block_touch_indices(
    frame: pl.DataFrame,
    *,
    direction: str,
    top: float,
    bottom: float,
    created_index: int,
) -> tuple[int | None, int | None]:
    ohlc = _normalize_ohlcv(frame)
    high = _series_to_float_array(ohlc["high"])
    low = _series_to_float_array(ohlc["low"])
    start = created_index + 1
    if start >= ohlc.height:
        return None, None

    intersects = (low[start:] <= top) & (high[start:] >= bottom)
    mitigation_index = (
        int(np.argmax(intersects) + start) if np.any(intersects) else None
    )

    if direction == "long":
        invalidation_mask = low[start:] < bottom
    else:
        invalidation_mask = high[start:] > top
    invalidation_index = (
        int(np.argmax(invalidation_mask) + start) if np.any(invalidation_mask) else None
    )
    return mitigation_index, invalidation_index


def _liquidity_state(
    frame: pl.DataFrame,
    *,
    raw_direction: float,
    level: float,
    swept_index: int | None,
) -> tuple[ZoneState, int | None]:
    if swept_index is None:
        return "fresh", None
    ohlc = _normalize_ohlcv(frame)
    close = _series_to_float_array(ohlc["close"])
    start = swept_index + 1
    if start >= ohlc.height:
        return "mitigated", None
    if raw_direction > 0:
        invalidation_mask = close[start:] > level
    else:
        invalidation_mask = close[start:] < level
    invalidation_index = (
        int(np.argmax(invalidation_mask) + start) if np.any(invalidation_mask) else None
    )
    return _zone_state(swept_index, invalidation_index), invalidation_index


def latest_fvg_zone(
    frame: pl.DataFrame,
    *,
    join_consecutive: bool = True,
    allowed_states: tuple[ZoneState, ...] = ("fresh", "mitigated"),
    current_price: float | None = None,
    touch_buffer: float = 0.0,
) -> SMCZone | None:
    zones = fvg(frame, join_consecutive=join_consecutive)
    for idx in range(zones.height - 1, -1, -1):
        raw_direction = zones.item(idx, "FVG")
        if _is_missing(raw_direction):
            continue
        top = float(zones.item(idx, "Top"))
        bottom = float(zones.item(idx, "Bottom"))
        direction = "long" if float(raw_direction) > 0 else "short"
        mitigation_index = _optional_index(zones.item(idx, "MitigatedIndex"))
        invalidation_index = _fvg_invalidation_index(
            frame,
            direction=direction,
            top=top,
            bottom=bottom,
            created_index=idx,
        )
        state = _zone_state(mitigation_index, invalidation_index)
        if state not in allowed_states:
            continue
        low = min(top, bottom)
        high = max(top, bottom)
        if current_price is not None:
            if not ((low - touch_buffer) <= current_price <= (high + touch_buffer)):
                continue
        return SMCZone(
            kind="fvg",
            direction=direction,
            top=high,
            bottom=low,
            created_index=idx,
            state=state,
            midpoint=(high + low) / 2.0,
            width=high - low,
            mitigation_index=mitigation_index,
            invalidation_index=invalidation_index,
            metadata=cast(dict[str, float | int | str | None], _fvg_fill_metrics(
                frame,
                direction=direction,
                top=high,
                bottom=low,
                created_index=idx
            )
            ),
        )

def latest_order_block(
    frame: pl.DataFrame,
    *,
    swing_length: int = 5,
    mode: SMCMode = "live_safe",
    include_unconfirmed_tail: bool = False,
    allowed_states: tuple[ZoneState, ...] = ("fresh", "mitigated"),
    current_price: float | None = None,
    touch_buffer: float = 0.0,
) -> SMCZone | None:
    swings = swing_highs_lows(
        frame,
        swing_length=swing_length,
        mode=mode,
        include_unconfirmed_tail=include_unconfirmed_tail,
    )
    zones = order_blocks(frame, swings)
    for idx in range(zones.height - 1, -1, -1):
        raw_direction = zones.item(idx, "OB")
        if _is_missing(raw_direction):
            continue
        top = float(zones.item(idx, "Top"))
        bottom = float(zones.item(idx, "Bottom"))
        direction = "long" if float(raw_direction) > 0 else "short"
        mitigation_index, invalidation_index = _order_block_touch_indices(
            frame,
            direction=direction,
            top=top,
            bottom=bottom,
            created_index=idx,
        )
        state = _zone_state(mitigation_index, invalidation_index)
        if state not in allowed_states:
            continue
        low = min(top, bottom)
        high = max(top, bottom)
        if current_price is not None:
            if not ((low - touch_buffer) <= current_price <= (high + touch_buffer)):
                continue
        return SMCZone(
            kind="order_block",
            direction=direction,
            top=high,
            bottom=low,
            created_index=idx,
            state=state,
            midpoint=(high + low) / 2.0,
            width=high - low,
            mitigation_index=mitigation_index,
            invalidation_index=invalidation_index,
            metadata={
                "ob_volume": float(zones.item(idx, "OBVolume")),
                "strength_pct": float(zones.item(idx, "Percentage")),
                "age_bars": frame.height - idx - 1,
                "mitigation_state": state,
            },
        )
    return None


def latest_structure_break(
    frame: pl.DataFrame,
    *,
    swing_length: int = 5,
    mode: SMCMode = "live_safe",
    close_break: bool = True,
    prefer_kind: Literal["choch", "bos"] = "choch",
) -> SMCZone | None:
    swings = swing_highs_lows(frame, swing_length=swing_length, mode=mode)
    structure = bos_choch(frame, swings, close_break=close_break)
    primary = "CHOCH" if prefer_kind == "choch" else "BOS"
    secondary = "BOS" if primary == "CHOCH" else "CHOCH"

    for idx in range(structure.height - 1, -1, -1):
        raw_value = structure.item(idx, primary)
        kind = primary.lower()
        if _is_missing(raw_value):
            raw_value = structure.item(idx, secondary)
            kind = secondary.lower()
        if _is_missing(raw_value):
            continue
        level = float(structure.item(idx, "Level"))
        broken_index = _optional_index(structure.item(idx, "BrokenIndex"))
        if broken_index is None:
            continue
        direction = "long" if float(raw_value) > 0 else "short"
        return SMCZone(
            kind=kind,
            direction=direction,
            top=level,
            bottom=level,
            created_index=idx,
            state="mitigated",
            midpoint=level,
            width=0.0,
            level=level,
            broken_index=broken_index,
        )
    return None


def latest_liquidity_sweep(
    frame: pl.DataFrame,
    *,
    swing_length: int = 5,
    mode: SMCMode = "live_safe",
    range_percent: float = 0.01,
) -> SMCZone | None:
    swings = swing_highs_lows(frame, swing_length=swing_length, mode=mode)
    pools = liquidity_pools(frame, swings, range_percent=range_percent)
    for idx in range(pools.height - 1, -1, -1):
        raw_direction = pools.item(idx, "Liquidity")
        if _is_missing(raw_direction):
            continue
        level = float(pools.item(idx, "Level"))
        end_index = _optional_index(pools.item(idx, "End"))
        swept_index = _optional_index(pools.item(idx, "Swept"))
        state, invalidation_index = _liquidity_state(
            frame,
            raw_direction=float(raw_direction),
            level=level,
            swept_index=swept_index,
        )
        trade_direction = "short" if float(raw_direction) > 0 else "long"
        return SMCZone(
            kind="liquidity_sweep",
            direction=trade_direction,
            top=level,
            bottom=level,
            created_index=idx,
            state=state,
            midpoint=level,
            width=0.0,
            level=level,
            end_index=end_index,
            sweep_index=swept_index,
            invalidation_index=invalidation_index,
            metadata={
                "pool_direction": "highs" if float(raw_direction) > 0 else "lows"
            },
        )
    return None


def latest_breaker_block(
    frame: pl.DataFrame,
    *,
    swing_length: int = 5,
    mode: SMCMode = "live_safe",
    current_price: float | None = None,
    retest_buffer: float = 0.0,
) -> SMCZone | None:
    swings = swing_highs_lows(frame, swing_length=swing_length, mode=mode)
    zones = order_blocks(frame, swings)
    for idx in range(zones.height - 1, -1, -1):
        raw_direction = zones.item(idx, "OB")
        if _is_missing(raw_direction):
            continue
        top = float(zones.item(idx, "Top"))
        bottom = float(zones.item(idx, "Bottom"))
        direction = "long" if float(raw_direction) > 0 else "short"
        mitigation_index, invalidation_index = _order_block_touch_indices(
            frame,
            direction=direction,
            top=top,
            bottom=bottom,
            created_index=idx,
        )
        if invalidation_index is None:
            continue
        low = min(top, bottom)
        high = max(top, bottom)
        if current_price is not None:
            if not ((low - retest_buffer) <= current_price <= (high + retest_buffer)):
                continue
        trade_direction = "short" if direction == "long" else "long"
        return SMCZone(
            kind="breaker_block",
            direction=trade_direction,
            top=high,
            bottom=low,
            created_index=idx,
            state="invalidated",
            midpoint=(high + low) / 2.0,
            width=high - low,
            mitigation_index=mitigation_index,
            invalidation_index=invalidation_index,
            metadata={
                "source_ob_direction": direction,
                "ob_volume": float(zones.item(idx, "OBVolume")),
                "strength_pct": float(zones.item(idx, "Percentage")),
            },
        )
    return None


__all__ = [
    "SMCZone",
    "SMCMode",
    "ZoneState",
    "bos_choch",
    "fvg",
    "latest_breaker_block",
    "latest_fvg_zone",
    "latest_liquidity_sweep",
    "latest_order_block",
    "latest_structure_break",
    "liquidity_pools",
    "order_blocks",
    "swing_highs_lows",
]

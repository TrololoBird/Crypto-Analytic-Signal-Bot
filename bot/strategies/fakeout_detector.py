from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..features.prepare import _swing_points
from ._common import confirmed_pattern_frame
from ._roadmap import _as_float, _build_atr_signal, _last, _prev, _reject
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["FakeoutDetectorSetup"]


def detect_fakeout(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    defaults = effective_params
    base_score = _as_float(
        defaults.get("base_score", defaults["base_score"]),
        defaults["base_score"],
    )
    min_volume_ratio = _as_float(
        defaults.get("min_volume_ratio", defaults["min_volume_ratio"]),
        defaults["min_volume_ratio"],
    )
    sl_buffer_atr = _as_float(
        defaults.get("sl_buffer_atr", defaults["sl_buffer_atr"]),
        defaults["sl_buffer_atr"],
    )
    min_rr = _as_float(defaults.get("min_rr", defaults["min_rr"]), defaults["min_rr"])
    fakeout_lookback = max(
        5,
        int(
            _as_float(
                defaults.get(
                    "fakeout_lookback_bars",
                    defaults["fakeout_lookback_bars"],
                ),
                defaults["fakeout_lookback_bars"],
            )
        ),
    )
    fakeout_window = max(
        1,
        int(
            _as_float(
                defaults.get(
                    "fakeout_window_bars",
                    defaults["fakeout_window_bars"],
                ),
                defaults["fakeout_window_bars"],
            )
        ),
    )

    w = confirmed_pattern_frame(prepared.work_15m)
    if w.height < 30:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w.height)
        return None

    atr = _last(w, "atr14")
    if atr <= 0:
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    lookback_tail = w.tail(min(fakeout_lookback, w.height))
    sh_mask, sl_mask = _swing_points(lookback_tail, n=2, include_unconfirmed_tail=True)
    swing_highs = lookback_tail.filter(sh_mask)["high"]
    swing_lows = lookback_tail.filter(sl_mask)["low"]

    tail = w.tail(min(fakeout_window + 1, w.height))

    direction = None
    level = 0.0
    sweep_extreme = 0.0
    breakout_idx = -1
    reasons_list: list[str] = []

    for sh_cell in swing_highs:
        sh = _as_float(sh_cell)
        if sh <= 0:
            continue
        for row_idx in range(tail.height):
            bar_high = _as_float(tail.item(row_idx, "high"))
            bar_close = _as_float(tail.item(row_idx, "close"))
            if bar_high > sh and bar_close < sh:
                direction = "short"
                level = sh
                sweep_extreme = bar_high
                breakout_idx = row_idx
                reasons_list.append(f"short_fakeout_swing_high={sh:.4f}")
                break
        if direction is not None:
            break

    if direction is None:
        for sl_cell in swing_lows:
            sl = _as_float(sl_cell)
            if sl <= 0:
                continue
            for row_idx in range(tail.height):
                bar_low = _as_float(tail.item(row_idx, "low"))
                bar_close = _as_float(tail.item(row_idx, "close"))
                if bar_low < sl and bar_close > sl:
                    direction = "long"
                    level = sl
                    sweep_extreme = bar_low
                    breakout_idx = row_idx
                    reasons_list.append(f"long_fakeout_swing_low={sl:.4f}")
                    break
            if direction is not None:
                break

    if direction is None:
        _reject(prepared, setup_id, "fakeout_pattern_missing")
        return None

    if breakout_idx >= 0 and "volume_ratio20" in tail.columns:
        breakout_vol = _as_float(tail.item(breakout_idx, "volume_ratio20"), 0.0)
        if breakout_vol < min_volume_ratio:
            _reject(
                prepared,
                setup_id,
                "breakout_volume_too_low",
                breakout_vol=breakout_vol,
                min_volume_ratio=min_volume_ratio,
            )
            return None

    clarity = _as_float(
        defaults.get("structure_clarity", defaults["structure_clarity"]),
        defaults["structure_clarity"],
    )
    if direction == "long":
        proximity = abs(level - sweep_extreme) / atr if atr > 0 else 1.0
    else:
        proximity = abs(sweep_extreme - level) / atr if atr > 0 else 1.0
    if 0 < proximity < 0.5:
        boost = (0.5 - proximity) / 0.5 * 0.15
        clarity = min(1.0, clarity + boost)
        reasons_list.append(f"proximity={proximity:.3f}")

    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=defaults,
        confirmed_bar=True,
        reasons=[
            f"fakeout_{direction}",
            f"vol_ratio={breakout_vol:.2f}",
            f"level={level:.4f}",
            *reasons_list,
        ],
        family=family,
        structure_clarity=clarity,
        entry_anchor=level if level > 0.0 else None,
        stop_anchor=sweep_extreme if sweep_extreme > 0.0 else None,
    )


class FakeoutDetectorSetup(RoadmapSetup):
    setup_id = "fakeout_detector"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "base_score": 0.52,
        "min_volume_ratio": 1.0,
        "sl_buffer_atr": 0.65,
        "min_rr": 1.8,
        "fakeout_lookback_bars": 20,
        "fakeout_window_bars": 3,
        "structure_clarity": 0.5,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_fakeout(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["FakeoutDetectorSetup"]

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, ClassVar

from ..features.prepare import _swing_points
from ._common import confirmed_pattern_frame
from ._roadmap import _as_float, _build_atr_signal, _prev, _reject
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger(__name__)

__all__ = ["detect_pinbar_reversal"]


def detect_pinbar_reversal(
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
    wick_to_body_min = _as_float(
        defaults.get("wick_to_body_min", defaults["wick_to_body_min"]),
        defaults["wick_to_body_min"],
    )
    wick_to_atr_min = _as_float(
        defaults.get("wick_to_atr_min", defaults["wick_to_atr_min"]),
        defaults["wick_to_atr_min"],
    )
    body_to_atr_max = _as_float(
        defaults.get("body_to_atr_max", defaults["body_to_atr_max"]),
        defaults["body_to_atr_max"],
    )
    swing_touch_boost = _as_float(
        defaults.get("swing_touch_boost", defaults["swing_touch_boost"]),
        defaults["swing_touch_boost"],
    )

    raw = prepared.work_15m
    w = confirmed_pattern_frame(raw)
    if w.height < 20:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w.height)
        return None

    if raw.height < 2:
        _reject(prepared, setup_id, "insufficient_closed_bars")
        return None

    atr = _prev(raw, "atr14")
    if atr <= 0 or math.isnan(atr):
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    open_p = _prev(raw, "open")
    high_p = _prev(raw, "high")
    low_p = _prev(raw, "low")
    close_p = _prev(raw, "close")

    body = abs(close_p - open_p)
    upper_wick = high_p - max(open_p, close_p)
    lower_wick = min(open_p, close_p) - low_p

    if body <= 0 or body > atr * body_to_atr_max:
        _reject(
            prepared,
            setup_id,
            "body_too_large_or_zero",
            body=body,
            body_to_atr_max=body_to_atr_max,
        )
        return None

    long_pin = lower_wick >= body * wick_to_body_min and lower_wick >= atr * wick_to_atr_min
    short_pin = upper_wick >= body * wick_to_body_min and upper_wick >= atr * wick_to_atr_min

    if not long_pin and not short_pin:
        _reject(
            prepared,
            setup_id,
            "no_pin_bar",
            lower_wick=lower_wick,
            upper_wick=upper_wick,
            body=body,
        )
        return None

    if long_pin and short_pin:
        direction = "long" if lower_wick > upper_wick else "short"
    elif long_pin:
        direction = "long"
    else:
        direction = "short"

    vol_ratio = _prev(raw, "volume_ratio20", 1.0)

    if vol_ratio < min_volume_ratio:
        _reject(
            prepared,
            setup_id,
            "volume_too_low",
            vol_ratio=vol_ratio,
            min_volume_ratio=min_volume_ratio,
        )
        return None

    reasons: list[str] = []
    swing_touch = False
    w1h = confirmed_pattern_frame(prepared.work_1h)
    if w1h.height > 5:
        sh_mask, sl_mask = _swing_points(w1h, n=3, include_unconfirmed_tail=False)
        if direction == "long":
            swing_lows = w1h.filter(sl_mask)["low"]
            for sl_price in swing_lows:
                if abs(low_p - float(sl_price)) <= atr * 0.3:
                    swing_touch = True
                    reasons.append(f"wick_touches_swing_low={float(sl_price):.4f}")
                    break
        else:
            swing_highs = w1h.filter(sh_mask)["high"]
            for sh_price in swing_highs:
                if abs(high_p - float(sh_price)) <= atr * 0.3:
                    swing_touch = True
                    reasons.append(f"wick_touches_swing_high={float(sh_price):.4f}")
                    break

    no_htf_level_penalty = _as_float(
        defaults.get("no_htf_level_penalty", defaults.get("no_htf_level_penalty", 0.70)),
        0.70,
    )
    adjusted_base = base_score + (swing_touch_boost if swing_touch else 0.0)
    if not swing_touch:
        adjusted_base *= no_htf_level_penalty
        reasons.append(f"no_htf_swing_level_penalty={no_htf_level_penalty:.2f}")

    reasons.append(
        f"pinbar_{direction}: body={body:.4f} upper_wick={upper_wick:.4f} "
        f"lower_wick={lower_wick:.4f}"
    )
    if swing_touch:
        reasons.append(f"swing_touch_boost={swing_touch_boost:.2f}")

    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params={
            "base_score": adjusted_base,
            "sl_buffer_atr": sl_buffer_atr,
            "min_rr": min_rr,
        },
        reasons=reasons,
        family=family,
        timeframe="15m+1h",
        confirmed_bar=True,
        entry_anchor=low_p if direction == "long" else high_p,
    )


class PinbarReversalSetup(RoadmapSetup):
    setup_id = "pinbar_reversal"
    ENTRY_ORDER_TYPE: ClassVar[str] = "limit"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "base_score": 0.52,
        "min_volume_ratio": 0.75,
        "sl_buffer_atr": 0.7,
        "min_rr": 1.8,
        "wick_to_body_min": 2.0,
        "wick_to_atr_min": 0.3,
        "body_to_atr_max": 0.25,
        "swing_touch_boost": 0.10,
        "no_htf_level_penalty": 0.70,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_pinbar_reversal(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["PinbarReversalSetup"]

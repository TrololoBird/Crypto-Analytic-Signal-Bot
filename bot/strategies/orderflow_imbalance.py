from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, ClassVar

from ..setups import _reject
from ._common import as_float as _as_float, confirmed_pattern_frame, last as _last
from ._roadmap import _build_atr_signal, _flow_delta_with_source, _prev
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger(__name__)


def detect_orderflow_imbalance(
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
    delta_z_threshold = _as_float(
        defaults.get("delta_z_threshold", defaults["delta_z_threshold"]),
        defaults["delta_z_threshold"],
    )
    score_boost_per_z = _as_float(
        defaults.get("score_boost_per_z", defaults["score_boost_per_z"]),
        defaults["score_boost_per_z"],
    )

    delta_value, delta_source = _flow_delta_with_source(prepared)
    if delta_value is None:
        _reject(prepared, setup_id, "delta_missing")
        return None

    w = confirmed_pattern_frame(prepared.work_15m)
    if w.height < 48:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w.height)
        return None

    if "delta_ratio" not in w.columns:
        _reject(prepared, setup_id, "delta_ratio_missing")
        return None

    delta_tail = w.tail(48)["delta_ratio"].drop_nulls()
    if delta_tail.len() < 20:
        _reject(prepared, setup_id, "insufficient_delta_data", bars=delta_tail.len())
        return None

    delta_mean = _as_float(delta_tail.mean(), 0.5)
    delta_std = _as_float(delta_tail.std(), 0.0)
    if delta_std <= 0:
        _reject(prepared, setup_id, "delta_std_zero")
        return None

    current_delta = _as_float(w.item(-1, "delta_ratio"), 0.5)
    delta_z = (current_delta - delta_mean) / delta_std

    vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
    if vol_ratio < min_volume_ratio:
        _reject(
            prepared,
            setup_id,
            "volume_too_low",
            vol_ratio=vol_ratio,
            min_volume_ratio=min_volume_ratio,
        )
        return None

    atr = _last(prepared.work_15m, "atr14")
    if atr <= 0 or math.isnan(atr):
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    lookback = 10
    recent_high = _as_float(w["high"].tail(lookback).max())
    recent_low = _as_float(w["low"].tail(lookback).min())
    current_high = _last(prepared.work_15m, "high")
    current_low = _last(prepared.work_15m, "low")

    if delta_z > delta_z_threshold and current_high < recent_high:
        direction = "short"
    elif delta_z < -delta_z_threshold and current_low > recent_low:
        direction = "long"
    else:
        _reject(
            prepared,
            setup_id,
            "divergence_not_confirmed",
            delta_z=round(delta_z, 3),
            delta_z_threshold=delta_z_threshold,
            current_high=current_high,
            current_low=current_low,
        )
        return None

    z_magnitude = abs(delta_z)
    score_boost = 1.0 + min(max(z_magnitude * score_boost_per_z, 0.10), 0.30)
    adjusted_params = {**effective_params, "base_score": base_score * score_boost}

    reasons = [
        f"Orderflow imbalance {direction}: delta_z={delta_z:.3f} source={delta_source}",
        f"threshold={delta_z_threshold:.2f}",
        f"vol_ratio={vol_ratio:.2f}",
        f"z_boost={score_boost:.4f}",
    ]

    entry_anchor = _prev(prepared.work_15m, "ema20", 0.0) or None
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=adjusted_params,
        reasons=reasons,
        family=family,
        timeframe="15m",
        entry_anchor=entry_anchor,
    )


class OrderflowImbalanceSetup(RoadmapSetup):
    setup_id = "orderflow_imbalance"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "base_score": 0.55,
        "min_volume_ratio": 0.85,
        "delta_z_threshold": 1.5,
        "score_boost_per_z": 0.08,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_orderflow_imbalance(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["OrderflowImbalanceSetup"]

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ._common import confirmed_pattern_frame
from ._roadmap import (
    _as_float,
    _build_atr_signal,
    _prev,
    _reject,
)
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_cvd_exhaustion"]


def detect_cvd_exhaustion(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    w = confirmed_pattern_frame(prepared.work_15m)
    if w.height < 30:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w.height)
        return None

    atr = _as_float(w.item(-1, "atr14"))
    if atr <= 0:
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    cvd_col: str | None = (
        "cvd" if "cvd" in w.columns else ("delta_ratio" if "delta_ratio" in w.columns else None)
    )
    if cvd_col is None:
        _reject(prepared, setup_id, "cvd_column_missing")
        return None

    lookback = max(3, int(params["cvd_lookback_bars"]))
    if w.height < lookback + 1:
        _reject(prepared, setup_id, "insufficient_lookback_bars", lookback=lookback)
        return None

    last_n = w.tail(lookback)
    close_vals = [float(v) for v in last_n["close"].to_list()]
    cvd_vals = [float(v) for v in last_n[cvd_col].to_list() if v is not None]
    if len(cvd_vals) < lookback // 2:
        _reject(prepared, setup_id, "insufficient_cvd_data", valid=len(cvd_vals))
        return None

    n = len(close_vals)
    x_bar = (n - 1) / 2.0
    close_y_bar = sum(close_vals) / n
    cvd_y_bar = sum(cvd_vals) / n
    price_num = 0.0
    cvd_num = 0.0
    den = 0.0
    for i in range(n):
        dx = i - x_bar
        price_num += dx * (close_vals[i] - close_y_bar)
        cvd_num += dx * (cvd_vals[i] - cvd_y_bar)
        den += dx * dx
    price_slope = price_num / den if den != 0 else 0.0
    cvd_slope = cvd_num / den if den != 0 else 0.0

    min_div = float(params["cvd_divergence_min"])
    if price_slope > 0 and cvd_slope < -min_div:
        direction = "short"
    elif price_slope < 0 and cvd_slope > min_div:
        direction = "long"
    else:
        _reject(
            prepared,
            setup_id,
            "cvd_divergence_not_detected",
            price_slope=round(price_slope, 6),
            cvd_slope=round(cvd_slope, 6),
        )
        return None

    w1h = confirmed_pattern_frame(prepared.work_1h)
    if w1h.height < 3:
        _reject(prepared, setup_id, "insufficient_1h_bars", bars=w1h.height)
        return None
    adx = _as_float(w1h.item(-1, "adx14"))
    max_adx = float(params["max_adx"])
    if adx >= max_adx:
        _reject(prepared, setup_id, "adx_too_high", adx=adx, max_adx=max_adx)
        return None

    volume_penalty = False
    if "volume" in w.columns:
        v5 = [float(v) for v in w["volume"].tail(5).to_list() if v is not None and float(v) > 0]
        v20 = [float(v) for v in w["volume"].tail(20).to_list() if v is not None and float(v) > 0]
        if v5 and v20:
            avg5 = sum(v5) / len(v5)
            avg20 = sum(v20) / len(v20)
            vol_ratio = avg5 / avg20 if avg20 > 0 else 1.0
            if vol_ratio < float(params["min_volume_ratio"]):
                volume_penalty = True
        else:
            volume_penalty = True
    else:
        volume_penalty = True

    reasons = [
        f"cvd_exhaustion_{direction}",
        f"price_slope={price_slope:.6f}",
        f"cvd_slope={cvd_slope:.6f}",
        f"adx_1h={adx:.1f}",
        f"cvd_source={cvd_col}",
    ]
    if volume_penalty:
        reasons.append("volume_decline_penalty")

    entry_anchor = _prev(w, "ema20", 0.0) or None

    structure_clarity = 0.60 if not volume_penalty else 0.50

    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        confirmed_bar=True,
        entry_anchor=entry_anchor,
        reasons=reasons,
        family=family,
        structure_clarity=structure_clarity,
    )


class CVDExhaustionSetup(RoadmapSetup):
    setup_id = "cvd_exhaustion"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "base_score": 0.52,
        "min_volume_ratio": 0.70,
        "sl_buffer_atr": 0.6,
        "min_rr": 1.8,
        "cvd_lookback_bars": 12,
        "max_adx": 30.0,
        "cvd_divergence_min": 0.02,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_cvd_exhaustion(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["CVDExhaustionSetup"]

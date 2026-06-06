from __future__ import annotations

import math
from typing import TYPE_CHECKING, ClassVar

from ..features.prepare import _swing_points
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.utils import build_structural_targets
from ._common import as_float as _as_float, confirmed_pattern_frame
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_supertrend_follow"]


def detect_supertrend_follow(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    work_15m = confirmed_pattern_frame(prepared.work_15m)
    work_1h = confirmed_pattern_frame(prepared.work_1h)
    if work_15m.height < 30 or work_1h.height < 30:
        _reject(prepared, setup_id, "insufficient_bars")
        return None

    required_15m = (
        "open",
        "close",
        "low",
        "high",
        "ema20",
        "atr14",
        "supertrend",
        "supertrend_dir",
        "volume_ratio20",
        "rsi14",
    )
    required_1h = ("adx14", "supertrend_dir")
    missing = [column for column in required_15m if column not in work_15m.columns]
    missing.extend(column for column in required_1h if column not in work_1h.columns)
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None

    close = _as_float(work_15m.item(-1, "close"))
    low = _as_float(work_15m.item(-1, "low"))
    high = _as_float(work_15m.item(-1, "high"))
    ema20 = _as_float(work_15m.item(-1, "ema20"))
    atr = _as_float(work_15m.item(-1, "atr14"))
    supertrend_line = _as_float(work_15m.item(-1, "supertrend"))
    vol_ratio = _as_float(work_15m.item(-1, "volume_ratio20"), 1.0)
    rsi = _as_float(work_15m.item(-1, "rsi14"), 50.0)
    st_15m = _as_float(work_15m.item(-1, "supertrend_dir"))
    st_1h = _as_float(work_1h.item(-1, "supertrend_dir"))
    adx_1h = _as_float(work_1h.item(-1, "adx14"))

    if min(close, low, high, ema20, supertrend_line, atr) <= 0.0 or math.isnan(atr):
        _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
        return None

    if adx_1h > 0.0 and adx_1h < float(effective_params["min_adx_1h"]):
        _reject(prepared, setup_id, "adx_too_low", adx_1h=adx_1h)
        return None

    volume_penalty = vol_ratio < float(effective_params["min_volume_ratio"])

    # FIX 2026-05-21: the detector silently capped the configured 0.65 ATR
    # pullback to 0.50 and checked only close-to-line distance. SuperTrend
    # retests are often wick touches that close back in trend direction, so
    # the candle range must participate in the confirmation.
    configured_pullback = float(
        effective_params.get(
            "pullback_atr_threshold",
            effective_params["ema_pullback_atr"],
        )
    )
    pullback_atr = max(0.20, min(configured_pullback, 2.0))
    bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
    direction: str | None = None
    stop_basis: float = 0.0
    entry_basis: float = 0.0
    confirmation_mode = ""

    line_buffer = atr * pullback_atr
    close_near_line = abs(close - supertrend_line) <= line_buffer
    long_retest = (close_near_line or low <= supertrend_line + line_buffer) and (
        close > supertrend_line
    )
    short_retest = (close_near_line or high >= supertrend_line - line_buffer) and (
        close < supertrend_line
    )
    if st_15m > 0 and st_1h > 0 and long_retest:
        direction = "long"
        stop_basis = min(low, supertrend_line)
        entry_basis = min(supertrend_line, close)
        confirmation_mode = "supertrend_line_retest"
    elif st_15m < 0 and st_1h < 0 and short_retest:
        direction = "short"
        stop_basis = max(high, supertrend_line)
        entry_basis = max(supertrend_line, close)
        confirmation_mode = "supertrend_line_retest"

    if direction is None:
        lookback = max(2, int(effective_params.get("ema_reclaim_lookback_bars", 6)))
        acceptance_atr = max(0.0, float(effective_params.get("ema_acceptance_atr", 0.35)))
        max_extension_atr = max(0.25, float(effective_params.get("max_ema_extension_atr", 1.25)))
        recent = work_15m.tail(min(lookback, work_15m.height))
        latest_close = close
        latest_ema = ema20
        latest_extension_atr = abs(latest_close - latest_ema) / atr
        if st_15m > 0 and st_1h > 0 and latest_extension_atr <= max_extension_atr:
            for local_idx in range(recent.height - 1, -1, -1):
                bar_open = _as_float(recent.item(local_idx, "open"))
                bar_low = _as_float(recent.item(local_idx, "low"))
                bar_close = _as_float(recent.item(local_idx, "close"))
                bar_ema = _as_float(recent.item(local_idx, "ema20"))
                if min(bar_open, bar_low, bar_close, bar_ema) <= 0.0:
                    continue
                touched_ema = bar_low <= bar_ema + atr * pullback_atr
                reclaimed_ema = latest_close >= latest_ema - atr * acceptance_atr
                directional_close = bar_close >= bar_open or latest_close >= latest_ema
                if touched_ema and reclaimed_ema and directional_close:
                    direction = "long"
                    stop_basis = min(bar_low, bar_ema)
                    entry_basis = min(latest_ema, latest_close)
                    confirmation_mode = f"ema20_reclaim_lag={recent.height - 1 - local_idx}"
                    break
        elif st_15m < 0 and st_1h < 0 and latest_extension_atr <= max_extension_atr:
            for local_idx in range(recent.height - 1, -1, -1):
                bar_open = _as_float(recent.item(local_idx, "open"))
                bar_high = _as_float(recent.item(local_idx, "high"))
                bar_close = _as_float(recent.item(local_idx, "close"))
                bar_ema = _as_float(recent.item(local_idx, "ema20"))
                if min(bar_open, bar_high, bar_close, bar_ema) <= 0.0:
                    continue
                touched_ema = bar_high >= bar_ema - atr * pullback_atr
                reclaimed_ema = latest_close <= latest_ema + atr * acceptance_atr
                directional_close = bar_close <= bar_open or latest_close <= latest_ema
                if touched_ema and reclaimed_ema and directional_close:
                    direction = "short"
                    stop_basis = max(bar_high, bar_ema)
                    entry_basis = max(latest_ema, latest_close)
                    confirmation_mode = f"ema20_reclaim_lag={recent.height - 1 - local_idx}"
                    break

    if direction is None:
        _reject(
            prepared,
            setup_id,
            "indicator.no_supertrend_pullback",
            st_15m=st_15m,
            st_1h=st_1h,
            distance_atr=abs(close - supertrend_line) / atr,
            low_line_distance_atr=abs(low - supertrend_line) / atr,
            high_line_distance_atr=abs(high - supertrend_line) / atr,
            pullback_atr=pullback_atr,
            ema_distance_atr=abs(close - ema20) / atr,
        )
        return None

    sh_mask, sl_mask = _swing_points(work_1h, n=3, include_unconfirmed_tail=True)
    min_rr = float(effective_params["min_rr"])
    price_anchor = entry_basis
    stop, tp1, tp2 = build_structural_targets(
        direction=direction,
        price_anchor=price_anchor,
        stop_basis=stop_basis,
        atr=atr,
        work_1h=work_1h,
        work_4h=prepared.work_4h,
        min_rr=min_rr,
        sl_buffer_atr=float(effective_params["sl_buffer_atr"]),
        sh_mask=sh_mask,
        sl_mask=sl_mask,
    )
    risk = abs(price_anchor - stop)
    if risk <= 0.0:
        _reject(prepared, setup_id, "invalid_stop", stop=stop, close=price_anchor)
        return None
    if tp1 is None or abs(tp1 - price_anchor) < risk * min_rr:
        tp1 = price_anchor + risk * min_rr if direction == "long" else price_anchor - risk * min_rr
    if tp2 is None or abs(tp2 - price_anchor) <= abs(tp1 - price_anchor):
        tp2 = (
            price_anchor + risk * max(2.0, min_rr + 0.35)
            if direction == "long"
            else price_anchor - risk * max(2.0, min_rr + 0.35)
        )

    base_score = float(effective_params["base_score"])
    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
        structure_clarity=0.68 if confirmation_mode.startswith("supertrend") else 0.52,
    )
    if confirmation_mode.startswith("ema20"):
        score *= float(effective_params.get("ema_pullback_score_penalty", 0.92))
    if volume_penalty:
        score *= float(effective_params.get("volume_penalty", 0.92))

    # Graded bias alignment
    if (direction == "long" and bias_1h == "downtrend") or (
        direction == "short" and bias_1h == "uptrend"
    ):
        score *= effective_params.get("bias_mismatch_penalty", 0.75)

    reasons = [
        f"supertrend_follow_{direction}",
        f"bias_1h={bias_1h}",
        f"st_15m={st_15m:.0f}",
        f"st_1h={st_1h:.0f}",
        f"supertrend_line={supertrend_line:.4f}",
        f"ema20={ema20:.4f}",
        confirmation_mode,
        f"adx_1h={adx_1h:.1f}",
        f"volume_ratio={vol_ratio:.2f}",
        f"limit_entry={price_anchor:.4f}",
    ]
    if volume_penalty:
        reasons.append("volume_confirmation_penalty")
    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe="15m+1h",
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=price_anchor,
        atr=atr,
    )


class SuperTrendFollowSetup(RoadmapSetup):
    setup_id = "supertrend_follow"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "base_score": 0.56,
        "min_adx_1h": 12.0,
        "min_volume_ratio": 1.0,
        "volume_penalty": 0.92,
        "pullback_atr_threshold": 0.65,
        "ema_pullback_atr": 0.65,
        "ema_acceptance_atr": 0.35,
        "ema_reclaim_lookback_bars": 6,
        "max_ema_extension_atr": 1.25,
        "sl_buffer_atr": 0.65,
        "min_rr": 1.9,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_supertrend_follow(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["SuperTrendFollowSetup"]

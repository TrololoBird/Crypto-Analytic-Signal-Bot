from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _as_float,
    _build_atr_signal,
    _missing_columns,
    _reject,
)
from .spec_patterns import build_spec_signal, detect_regular_divergence

class RSIDivergenceBottomSetup(RoadmapSetup):
    setup_id = "rsi_divergence_bottom"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "divergence_window": 12,
        "min_rsi_delta": 1.5,
        "min_price_delta_pct": 0.05,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        work = prepared.work_15m
        hit = detect_regular_divergence(work, timeframe="15m", require_oversold=True)
        if hit is not None:
            return build_spec_signal(
                prepared=prepared,
                settings=settings,
                setup_id=self.setup_id,
                family=self.family,
                hit=hit,
                defaults=self.DEFAULTS,
                params=params,
            )

        # FIX 2026-05-21: strict RSI spec can miss valid windowed divergence;
        # fall through to the existing configured detector before rejecting.
        missing = _missing_columns(work, ("high", "low", "close", "rsi14"))
        if missing:
            _reject(prepared, self.setup_id, "missing_columns", missing_fields=missing)
            return None
        window = int(params["divergence_window"])
        if work.height < window * 2:
            _reject(prepared, self.setup_id, "insufficient_divergence_window")
            return None
        previous = work.slice(work.height - window * 2, window)
        recent = work.tail(window)
        previous_lows = [_as_float(value) for value in previous["low"].to_list()]
        recent_lows = [_as_float(value) for value in recent["low"].to_list()]
        previous_highs = [_as_float(value) for value in previous["high"].to_list()]
        recent_highs = [_as_float(value) for value in recent["high"].to_list()]
        if not previous_lows or not recent_lows or not previous_highs or not recent_highs:
            _reject(prepared, self.setup_id, "rsi_divergence_missing")
            return None
        prev_low_idx = min(range(len(previous_lows)), key=previous_lows.__getitem__)
        recent_low_idx = min(range(len(recent_lows)), key=recent_lows.__getitem__)
        prev_high_idx = max(range(len(previous_highs)), key=previous_highs.__getitem__)
        recent_high_idx = max(range(len(recent_highs)), key=recent_highs.__getitem__)
        prev_low = previous_lows[prev_low_idx]
        recent_low = recent_lows[recent_low_idx]
        prev_high = previous_highs[prev_high_idx]
        recent_high = recent_highs[recent_high_idx]
        prev_rsi_low = _as_float(previous.item(prev_low_idx, "rsi14"), 50.0)
        recent_rsi_low = _as_float(recent.item(recent_low_idx, "rsi14"), 50.0)
        prev_rsi_high = _as_float(previous.item(prev_high_idx, "rsi14"), 50.0)
        recent_rsi_high = _as_float(recent.item(recent_high_idx, "rsi14"), 50.0)
        price_delta = float(params["min_price_delta_pct"]) / 100.0
        rsi_delta = float(params["min_rsi_delta"])
        if (
            recent_low < prev_low * (1.0 - price_delta)
            and recent_rsi_low >= prev_rsi_low + rsi_delta
        ):
            direction = "long"
        elif (
            recent_high > prev_high * (1.0 + price_delta)
            and recent_rsi_high <= prev_rsi_high - rsi_delta
        ):
            direction = "short"
        else:
            _reject(prepared, self.setup_id, "rsi_divergence_missing")
            return None
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"rsi_divergence_{direction}",
                f"price_low={prev_low:.4f}->{recent_low:.4f}",
                f"price_high={prev_high:.4f}->{recent_high:.4f}",
                (
                    f"rsi_low={prev_rsi_low:.1f}->{recent_rsi_low:.1f} "
                    f"rsi_high={prev_rsi_high:.1f}->{recent_rsi_high:.1f}"
                ),
            ],
            family=self.family,
            structure_clarity=0.7,
        )


__all__ = ["RSIDivergenceBottomSetup"]

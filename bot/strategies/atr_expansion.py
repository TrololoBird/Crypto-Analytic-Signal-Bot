from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _as_float,
    _build_atr_signal,
    _last,
    _missing_columns,
    _reject,
    _series_mean_tail,
)
from .spec_patterns import build_spec_signal, detect_atr_expansion

class ATRExpansionSetup(RoadmapSetup):
    setup_id = "atr_expansion"
    family = "volatility"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "atr_mean_window": 20,
        "min_atr_expansion_ratio": 1.08,
        "min_body_atr": 0.25,
        "signal_lookback_bars": 6,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        hit = detect_atr_expansion(prepared.work_15m, timeframe="15m")
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

        # FIX 2026-05-21: spec requires a large current-bar TR spike; if absent,
        # use the existing config-driven recent ATR expansion detector below.
        work = prepared.work_15m
        missing = _missing_columns(work, ("open", "high", "low", "close", "atr14"))
        if missing:
            _reject(prepared, self.setup_id, "missing_columns", missing_fields=missing)
            return None
        candidate = self._find_expansion_candidate(work, params, timeframe="15m")
        if candidate is None and not prepared.work_1h.is_empty():
            candidate = self._find_expansion_candidate(prepared.work_1h, params, timeframe="1h")
        if candidate is None:
            _reject(
                prepared,
                self.setup_id,
                "atr_expansion_too_low",
                min_atr_expansion_ratio=float(params["min_atr_expansion_ratio"]),
                min_body_atr=float(params["min_body_atr"]),
            )
            return None
        direction = str(candidate["direction"])
        ratio = float(candidate["ratio"])
        body_atr = float(candidate["body_atr"])
        signal_lag = int(candidate["signal_lag"])
        source_timeframe = str(candidate["timeframe"])
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"atr_expansion_{direction}",
                f"source_tf={source_timeframe}",
                f"atr_ratio={ratio:.2f}",
                f"body_atr={body_atr:.2f}",
                f"signal_lag={signal_lag}",
            ],
            family=self.family,
            timeframe=source_timeframe,
            structure_clarity=min((ratio - 1.0) / 1.0, 1.0),
        )

    def _find_expansion_candidate(
        self,
        work,
        params: dict[str, float],
        *,
        timeframe: str,
    ) -> dict[str, float | int | str] | None:
        missing = _missing_columns(work, ("open", "high", "low", "close", "atr14"))
        if missing or work.height < 25:
            return None
        lookback = max(1, int(params.get("signal_lookback_bars", 6)))
        recent = work.tail(min(lookback, work.height))
        atr = _last(work, "atr14")
        mean_atr = _series_mean_tail(work, "atr14", int(params["atr_mean_window"]))
        if atr <= 0.0 or mean_atr <= 0.0:
            return None

        range_window = work.tail(max(int(params["atr_mean_window"]) + lookback + 1, 25))
        true_ranges: list[float] = []
        previous_close = 0.0
        for idx in range(range_window.height):
            high = _as_float(range_window.item(idx, "high"))
            low = _as_float(range_window.item(idx, "low"))
            close = _as_float(range_window.item(idx, "close"))
            if high <= 0.0 or low <= 0.0:
                continue
            if previous_close > 0.0:
                true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
            else:
                true_range = high - low
            if true_range > 0.0:
                true_ranges.append(true_range)
            previous_close = close if close > 0.0 else previous_close
        mean_tr = sum(true_ranges[-int(params["atr_mean_window"]) :]) / max(
            1,
            len(true_ranges[-int(params["atr_mean_window"]) :]),
        )

        best: dict[str, float | int | str] | None = None
        for local_idx in range(recent.height):
            open_ = _as_float(recent.item(local_idx, "open"))
            high = _as_float(recent.item(local_idx, "high"))
            low = _as_float(recent.item(local_idx, "low"))
            close = _as_float(recent.item(local_idx, "close"))
            bar_atr = _as_float(recent.item(local_idx, "atr14"))
            if min(open_, high, low, close, bar_atr) <= 0.0:
                continue
            bar_range = high - low
            atr_ratio = bar_atr / mean_atr if mean_atr > 0.0 else 0.0
            tr_ratio = bar_range / mean_tr if mean_tr > 0.0 else 0.0
            ratio = max(atr_ratio, tr_ratio)
            body_atr = abs(close - open_) / max(bar_atr, bar_range, 1e-12)
            close_position = (close - low) / max(high - low, 1e-12)
            decisive_close = close_position >= 0.62 or close_position <= 0.38
            if ratio < float(params["min_atr_expansion_ratio"]):
                continue
            if body_atr < float(params["min_body_atr"]) and not (
                decisive_close and ratio >= float(params["min_atr_expansion_ratio"]) * 1.18
            ):
                continue
            score = ratio + body_atr + (0.10 if decisive_close else 0.0)
            if best is None or score > float(best["score"]):
                best = {
                    "score": score,
                    "ratio": ratio,
                    "body_atr": body_atr,
                    "direction": "long" if close >= open_ else "short",
                    "signal_lag": recent.height - 1 - local_idx,
                    "timeframe": timeframe,
                }
        return best


__all__ = ["ATRExpansionSetup"]

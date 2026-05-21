from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _build_atr_signal,
    _last,
    _missing_columns,
    _reject,
    _series_max_tail,
)
from .spec_patterns import build_spec_signal, detect_bb_squeeze_release

class BBSqueezeSetup(RoadmapSetup):
    setup_id = "bb_squeeze"
    family = "volatility"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "max_bb_width": 5.0,
        "min_volume_ratio": 0.90,
        "min_roc10_abs_pct": 0.10,
        "squeeze_release_lookback": 8.0,
        "squeeze_memory_bars": 20.0,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        hit = detect_bb_squeeze_release(prepared.work_15m, timeframe="15m")
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

        # FIX 2026-05-21: strict spec release is last-bar only; fall through to
        # the configured squeeze memory/release window before rejecting.
        work = prepared.work_15m
        missing = _missing_columns(work, ("bb_width", "squeeze_on", "squeeze_off", "roc10"))
        if missing:
            _reject(prepared, self.setup_id, "missing_columns", missing_fields=missing)
            return None
        bb_width = _last(work, "bb_width")
        release_lookback = int(params["squeeze_release_lookback"])
        memory_bars = int(params.get("squeeze_memory_bars", 20))
        prior = work.head(max(0, work.height - 1))
        squeeze_recent = _series_max_tail(prior, "squeeze_on", memory_bars)
        squeeze_release_recent = _series_max_tail(work, "squeeze_off", release_lookback)
        roc10 = _last(work, "roc10")
        vol_ratio = _last(work, "volume_ratio20", 1.0)
        if squeeze_recent <= 0.0 and bb_width > float(params["max_bb_width"]):
            _reject(prepared, self.setup_id, "bb_squeeze_not_active", bb_width=bb_width)
            return None
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        if squeeze_release_recent <= 0.0 and bb_width <= float(params["max_bb_width"]):
            squeeze_release_recent = 1.0
        if squeeze_release_recent <= 0.0:
            _reject(
                prepared,
                self.setup_id,
                "squeeze_breakout_unconfirmed",
                squeeze_release_recent=squeeze_release_recent,
                volume_ratio=vol_ratio,
                release_lookback=release_lookback,
            )
            return None
        if abs(roc10) < float(params["min_roc10_abs_pct"]):
            _reject(prepared, self.setup_id, "momentum_too_low", roc10=roc10)
            return None
        direction = "long" if roc10 > 0.0 else "short"
        clarity = min(abs(roc10), 1.0) * (0.90 if volume_penalty else 1.0)
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"bb_squeeze_{direction}",
                f"bb_width={bb_width:.2f}",
                f"release_recent={squeeze_release_recent:.0f}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


__all__ = ["BBSqueezeSetup"]

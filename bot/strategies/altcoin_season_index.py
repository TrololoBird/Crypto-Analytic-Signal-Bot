from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _build_atr_signal,
    _finite_or_none,
    _last,
    _price_change_pct,
    _reject,
)

class AltcoinSeasonIndexSetup(RoadmapSetup):
    setup_id = "altcoin_season_index"
    family = "multi_asset"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "altseason_long_threshold": 55.0,
        "btc_dominance_threshold": 45.0,
        "min_volume_ratio": 0.80,
        "min_roc10_abs_pct": 0.10,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        base = str(prepared.universe.base_asset or "").upper()
        if not base:
            _reject(prepared, self.setup_id, "data.base_asset_missing")
            return None
        if base in {"BTC", "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "XAU", "XAG"}:
            _reject(prepared, self.setup_id, "pattern.not_altcoin")
            return None
        index = _finite_or_none(getattr(prepared, "altcoin_season_index", None))
        if index is None:
            _reject(prepared, self.setup_id, "data.altcoin_season_index_missing")
            return None
        alt_index = index
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        roc10 = _last(prepared.work_15m, "roc10", _price_change_pct(prepared.work_15m, 10))
        if (
            alt_index >= float(params["altseason_long_threshold"])
            and prepared.bias_1h != "downtrend"
        ):
            direction = "long"
        elif (
            alt_index <= float(params["btc_dominance_threshold"]) and prepared.bias_1h != "uptrend"
        ):
            direction = "short"
        elif abs(roc10) >= float(params["min_roc10_abs_pct"]) and prepared.bias_1h in {
            "uptrend",
            "downtrend",
        }:
            direction = "long" if prepared.bias_1h == "uptrend" else "short"
        else:
            _reject(
                prepared,
                self.setup_id,
                "altcoin_phase_not_actionable",
                altcoin_season_index=alt_index,
            )
            return None
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"altcoin_season_{direction}",
                f"alt_index={alt_index:.1f}",
                f"roc10={roc10:.2f}",
            ],
            family=self.family,
            structure_clarity=max(abs(alt_index - 50.0) / 50.0, min(abs(roc10), 1.0))
            * (0.90 if volume_penalty else 1.0),
        )


__all__ = ["AltcoinSeasonIndexSetup"]

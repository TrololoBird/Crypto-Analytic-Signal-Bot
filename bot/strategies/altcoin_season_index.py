from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup


from ._roadmap import (
    _build_atr_signal,
    _finite_or_none,
    _last,
    _price_change_pct,
    _reject,
)

__all__ = ["detect_altcoin_season_index"]


def detect_altcoin_season_index(
    prepared: PreparedSymbol,
    settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    base = str(prepared.universe.base_asset or "").upper()
    if not base:
        _reject(prepared, setup_id, "data.base_asset_missing")
        return None
    if base in {"BTC", "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "XAU", "XAG"}:
        _reject(prepared, setup_id, "pattern.not_altcoin")
        return None
    index = _finite_or_none(getattr(prepared, "altcoin_season_index", None))
    if index is None:
        _reject(prepared, setup_id, "data.altcoin_season_index_missing")
        return None
    alt_index = index
    vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
    volume_penalty = vol_ratio < float(params["min_volume_ratio"])
    roc10 = _last(prepared.work_15m, "roc10", _price_change_pct(prepared.work_15m, 10))
    symbol_change = float(getattr(prepared.universe, "price_change_pct", 0.0) or 0.0)
    btc_ctx = prepared.benchmark_context.get("BTCUSDT", {}) if prepared.benchmark_context else {}
    btc_change = _finite_or_none(btc_ctx.get("price_change_pct"))
    if btc_change is None:
        btc_change = _finite_or_none(getattr(prepared, "btc_change_24h", None))
    relative_vs_btc = symbol_change - float(btc_change) if btc_change is not None else roc10
    min_relative = float(params.get("min_relative_vs_btc_pct", 0.15))
    direction: str | None = None
    if (
        alt_index >= float(params["altseason_long_threshold"])
        and prepared.bias_1h != "downtrend"
        and relative_vs_btc >= min_relative
    ):
        direction = "long"
    elif (
        alt_index <= float(params["btc_dominance_threshold"])
        and prepared.bias_1h != "uptrend"
        and relative_vs_btc <= -min_relative
    ):
        direction = "short"
    elif (
        abs(roc10) >= float(params["min_roc10_abs_pct"])
        and prepared.bias_1h in {"uptrend", "downtrend"}
        and abs(relative_vs_btc) >= min_relative * 0.75
        and (
            (prepared.bias_1h == "uptrend" and relative_vs_btc > 0.0)
            or (prepared.bias_1h == "downtrend" and relative_vs_btc < 0.0)
        )
    ):
        direction = "long" if prepared.bias_1h == "uptrend" else "short"
    else:
        _reject(
            prepared,
            setup_id,
            "altcoin_phase_not_actionable",
            altcoin_season_index=alt_index,
            relative_vs_btc=relative_vs_btc,
        )
        return None
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        reasons=[
            f"altcoin_season_{direction}",
            f"alt_index={alt_index:.1f}",
            f"roc10={roc10:.2f}",
            f"relative_vs_btc={relative_vs_btc:.2f}",
        ],
        family=family,
        structure_clarity=max(abs(alt_index - 50.0) / 50.0, min(abs(roc10), 1.0))
        * (0.90 if volume_penalty else 1.0),
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
        "min_relative_vs_btc_pct": 0.15,
        "sl_buffer_atr": 1.20,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_altcoin_season_index(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["AltcoinSeasonIndexSetup"]

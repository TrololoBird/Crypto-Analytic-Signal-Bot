from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from hunt_core.analyst.pipeline._helpers import safe_float_opt
from hunt_core.analyst.pipeline.types import MarketRegime


@dataclass
class RegimeParameters:
    ker_min_trend: float = 0.45
    ker_max_caution: float = 0.30
    atr_multiplier_sl: float = 1.0
    sizing_pct: float = 1.0
    ttl_hours: float = 6.0
    block_longs: bool = False
    block_shorts: bool = False


REGIME_PARAMS: dict[MarketRegime, RegimeParameters] = {
    MarketRegime.NORMAL: RegimeParameters(),
    MarketRegime.HIGH_VOL: RegimeParameters(
        ker_min_trend=0.55,
        atr_multiplier_sl=1.5,
        sizing_pct=0.5,
        ttl_hours=4.0,
    ),
    MarketRegime.CRASH: RegimeParameters(
        ker_min_trend=0.65,
        atr_multiplier_sl=2.0,
        sizing_pct=0.25,
        ttl_hours=4.0,
        block_longs=True,
    ),
    MarketRegime.ALT_SEASON: RegimeParameters(
        ker_min_trend=0.40,
        sizing_pct=1.5,
        ttl_hours=6.0,
    ),
}


_cached_regime: tuple[MarketRegime, RegimeParameters, float] | None = None
_REFRESH_INTERVAL: float = 14400.0


def classify_market_regime(
    btc_data: dict[str, Any] | None = None,
    macro_data: Any = None,
    force: bool = False,
) -> tuple[MarketRegime, RegimeParameters]:
    global _cached_regime

    now = time.time()
    if not force and _cached_regime is not None and (now - _cached_regime[2]) < _REFRESH_INTERVAL:
        return _cached_regime[0], _cached_regime[1]

    btc = btc_data or {}
    btc_chg_24h = safe_float_opt(btc.get("chg_24h_pct") or btc.get("btc_chg_24h_pct"))
    btc_chg_7d = safe_float_opt(btc.get("chg_7d_pct"))
    btc_atr_pct = safe_float_opt(btc.get("atr_pct"))

    total3_chg = None
    btc_d_chg = None
    if macro_data is not None:
        total3_chg = getattr(macro_data, "total3_change_24h", None)
        btc_d_chg = getattr(macro_data, "btc_d_change_24h", None)

    regime = MarketRegime.NORMAL

    is_crash = (
        (btc_chg_24h is not None and btc_chg_24h < -12.0)
        or (btc_atr_pct is not None and btc_atr_pct > 5.5)
    )
    is_alt_season = (
        total3_chg is not None and btc_d_chg is not None
        and total3_chg > 10.0 and btc_d_chg < -2.5
    )
    is_high_vol = (
        (btc_chg_7d is not None and abs(btc_chg_7d) > 5.5)
        or (btc_atr_pct is not None and btc_atr_pct > 3.5)
    )

    if is_crash:
        regime = MarketRegime.CRASH
    elif is_alt_season:
        regime = MarketRegime.ALT_SEASON
    elif is_high_vol:
        regime = MarketRegime.HIGH_VOL

    params = REGIME_PARAMS.get(regime, REGIME_PARAMS[MarketRegime.NORMAL])
    _cached_regime = (regime, params, now)
    return regime, params


def clear_regime_cache() -> None:
    global _cached_regime
    _cached_regime = None

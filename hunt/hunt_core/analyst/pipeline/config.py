from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class MacroConfig:
    btc_ema_check: bool = True
    btc_ema_period: int = 50
    btc_candle_limit: int = 80
    btc_candle_min: int = 60

    btc_dominance_block_threshold: float = 1.0
    total3_drop_block_threshold: float = 2.0

    cmc_api_key: str = ""
    cmc_base_url: str = "https://pro-api.coinmarketcap.com/v1"
    cmc_cache_ttl: float = 1800.0

    caution_on_api_failure: bool = True


@dataclass
class TrendConfig:
    ker_min_trend: float = 0.45
    ker_strong_trend: float = 0.60
    ker_max_caution: float = 0.30
    ker_period: int = 10
    ema_slope_period: int = 3
    ema_period: int = 50
    slope_flat_caution: bool = True
    slope_flat_reduce_pct: float = 0.25


@dataclass
class PositioningConfig:
    funding_percentile_long_min: float = 0.03
    funding_percentile_short_min: float = 0.97
    funding_abs_threshold_8h: float = 0.001
    funding_history_days: int = 90
    funding_min_points: int = 30

    oi_rank_max_for_vp: int = 50
    oi_rank_cache_ttl: float = 300.0
    oi_min_value_usd: float = 10_000_000

    oi_divergence_threshold_pct: float = 10.0
    oi_divergence_price_threshold_pct: float = 2.0
    oi_divergence_lookback_hours: int = 24


@dataclass
class RiskConfig:
    atr_multiplier_sl_base: float = 1.5
    r_multiplier_tp1: float = 1.5
    r_multiplier_tp2: float = 2.5
    r_multiplier_tp3: float = 3.5
    base_sizing_pct: float = 1.0
    caution_sizing_pct: float = 0.5
    max_portfolio_heat_pct: float = 3.0
    correlation_reduce_factor: float = 0.5
    ttl_hours: float = 6.0
    ttl_high_vol_hours: float = 4.0
    ttl_low_vol_hours: float = 8.0
    min_rr_tp1: float = 0.4
    sl_min_pct: float = 1.5
    sl_max_pct: float = 5.0


@dataclass
class NewCoinConfig:
    min_age_days: int = 14
    min_candles_4h: int = 50
    min_candles_absolute: int = 20
    max_spread_pct: float = 0.5
    min_oi_usd: float = 10_000_000


@dataclass
class RegimeConfig:
    refresh_interval: float = 14400.0
    crash_btc_drop_24h: float = -10.0
    crash_atr_threshold: float = 5.0
    high_vol_btc_chg_7d: float = 5.0
    high_vol_atr_threshold: float = 3.0
    alt_season_total3_chg_7d: float = 10.0
    alt_season_btc_d_chg_7d: float = -2.0


@dataclass
class PipelineConfig:
    enabled: bool = True
    require_closed_candle: bool = True
    new_coin: NewCoinConfig = field(default_factory=NewCoinConfig)
    macro: MacroConfig = field(default_factory=MacroConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    positioning: PositioningConfig = field(default_factory=PositioningConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)

    _instance: PipelineConfig | None = None

    @classmethod
    def load(cls) -> PipelineConfig:
        if cls._instance is None:
            cls._instance = cls()
            cmc_key = os.environ.get("CMC_API_KEY", "")
            if cmc_key:
                cls._instance.macro.cmc_api_key = cmc_key
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

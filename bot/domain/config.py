from __future__ import annotations

import math
import os
import tomllib as _toml_lib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, field_validator, model_validator

from ..secrets import load_secrets


class RuntimeConfig(BaseModel):
    analysis_concurrency: int = Field(default=6, ge=1, le=20)
    strategy_concurrency: int = Field(default=4, ge=1, le=20)
    strategy_timeout_seconds: float = Field(default=12.0, ge=0.5, le=120.0)
    max_signals_per_cycle: int = Field(default=3, ge=1, le=10)
    event_bus_max_size: int = Field(default=512, ge=16, le=50_000)
    event_bus_warn_depth: int = Field(default=384, ge=8, le=50_000)
    event_bus_drop_log_interval: int = Field(default=25, ge=1, le=10_000)
    circuit_breaker_failure_threshold: int = Field(default=5, ge=0, le=100)
    metrics_host: str = "127.0.0.1"
    metrics_port: int = Field(default=9090, ge=1000, le=65535)
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8080, ge=1000, le=65535)
    dashboard_allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1",
            "http://localhost",
            "http://127.0.0.1:8080",
            "http://localhost:8080",
        ]
    )
    auto_open_dashboard: bool = False
    circuit_breaker_cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    telemetry_subdir: str = "telemetry"
    log_level: str = "INFO"
    logs_retention_days: int = Field(default=14, ge=1, le=3650)
    logs_max_files: int = Field(default=120, ge=10, le=5000)
    telemetry_retention_days: int = Field(default=14, ge=1, le=3650)
    telemetry_max_runs: int = Field(default=120, ge=10, le=5000)
    shortlist_refresh_interval_seconds: int = Field(default=7200, ge=300, le=86400)
    emergency_fallback_seconds: int = Field(default=1800, ge=300, le=7200)
    strict_data_quality: bool = True
    emit_strategy_routing_skips: bool = True
    diagnostic_trace_limit_per_symbol: int = Field(default=20, ge=0, le=500)
    # Startup throttling to prevent REST API flood
    startup_batch_size: int = Field(default=3, ge=1, le=10)
    startup_batch_delay_seconds: float = Field(default=2.0, ge=0.5, le=10.0)
    max_concurrent_rest_requests: int = Field(default=5, ge=1, le=20)
    emergency_context_warmup_timeout_seconds: float = Field(
        default=15.0, ge=1.0, le=120.0
    )
    emergency_context_warmup_symbol_limit: int = Field(default=12, ge=1, le=100)
    emergency_context_fetch_timeout_seconds: float = Field(default=3.0, ge=0.5, le=30.0)
    futures_data_request_limit_per_5m: int = Field(default=300, ge=30, le=1000)
    heartbeat_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return str(value or "INFO").strip().upper()


class UniverseConfig(BaseModel):
    quote_asset: str = "USDT"
    dynamic_limit: int = Field(default=60, ge=10, le=200)
    shortlist_limit: int = Field(
        default=45, ge=5, le=100
    )  # Reduced from 60 to prevent WS overload
    min_quote_volume_usd: float = Field(default=10_000_000.0, ge=0.0)
    min_listing_age_days: int = Field(default=14, ge=0, le=3650)
    light_refresh_interval_seconds: int = Field(default=75, ge=15, le=900)
    full_refresh_interval_seconds: int = Field(default=7200, ge=60, le=86_400)
    shortlist_spread_max_bps: float = Field(default=8.0, ge=0.5, le=100.0)
    shortlist_book_stale_seconds: float = Field(default=90.0, ge=5.0, le=3600.0)
    pinned_symbols: tuple[str, ...] = (
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
    )

    @field_validator("quote_asset")
    @classmethod
    def _normalize_quote_asset(cls, value: str) -> str:
        return str(value or "USDT").strip().upper()

    @field_validator("pinned_symbols")
    @classmethod
    def _normalize_pins(
        cls, value: tuple[str, ...] | list[str] | None
    ) -> tuple[str, ...]:
        return tuple(
            str(item).strip().upper() for item in (value or ()) if str(item).strip()
        )


class AssetConfig(BaseModel):
    """Per-symbol calibration overrides for priority asset routing."""

    primary_timeframe: Literal["5m", "15m", "1h", "4h"] = "15m"
    context_timeframes: tuple[Literal["5m", "15m", "1h", "4h"], ...] = ("1h", "4h")
    excluded_strategies: tuple[str, ...] = ()
    deep_analysis: bool = False

    @field_validator("excluded_strategies")
    @classmethod
    def _normalize_excluded_strategies(
        cls, value: tuple[str, ...] | list[str] | None
    ) -> tuple[str, ...]:
        return tuple(str(item).strip() for item in (value or ()) if str(item).strip())


class FilterConfig(BaseModel):
    """Runtime trading filters and stop placement heuristics.

    Stop distance is derived from the setup anchor plus/minus ATR multiplied by
    the setup-specific `stop_atr_multiplier_*` value. Higher multipliers widen
    stops and reduce premature exits at the cost of larger per-trade risk.
    """

    cooldown_minutes: int = Field(default=60, ge=0, le=1440)
    symbol_cooldown_minutes: int = Field(default=120, ge=0, le=10080)
    max_spread_bps: float = Field(default=8.0, ge=0.1, le=100.0)
    min_atr_pct: float = Field(default=0.40, ge=0.01, le=10.0)
    max_atr_pct: float = Field(default=10.0, ge=0.1, le=50.0)
    min_risk_reward: float = Field(default=1.9, ge=0.5, le=10.0)
    # Minimum ADX on 1h required for structure_pullback (trend strength gate).
    min_adx_1h: float = Field(default=20.0, ge=0.0, le=50.0)
    # Minimum final score after the simplified structure-based scoring.
    min_score: float = Field(default=0.66, ge=0.0, le=1.0)
    # Data freshness gates (avoid hidden "magic numbers" in filters.py).
    freshness_15m_minutes: int = Field(default=30, ge=5, le=240)
    freshness_1h_hours: int = Field(default=3, ge=1, le=48)
    freshness_4h_hours: int = Field(default=10, ge=1, le=240)
    min_bars_15m: int = Field(default=210, ge=30, le=5000)
    min_bars_1h: int = Field(default=210, ge=30, le=5000)
    min_bars_4h: int = Field(default=210, ge=30, le=5000)
    # Mark price sanity guard: reject if mark and last diverge beyond this pct.
    max_mark_price_deviation_pct: float = Field(default=0.005, ge=0.0, le=0.10)
    setups: dict[str, dict[str, float]] = Field(default_factory=dict)

    @field_validator("setups")
    @classmethod
    def _normalize_setup_overrides(
        cls, value: Mapping[str, Mapping[str, Any]] | None
    ) -> dict[str, dict[str, float]]:
        normalized: dict[str, dict[str, float]] = {}
        for setup_id, params in (value or {}).items():
            normalized_setup: dict[str, float] = {}
            for param_name, param_value in params.items():
                key = str(param_name)
                coerced = float(param_value)
                if not math.isfinite(coerced):
                    raise ValueError(f"filters.setups.{setup_id}.{key} must be finite")
                if key in {"sl_buffer_atr", "sl_atr_mult"} and coerced < 0.05:
                    raise ValueError(
                        f"filters.setups.{setup_id}.{key} must be >= 0.05 "
                        "(ATR fraction scale)"
                    )
                if key == "min_rr" and coerced < 0.5:
                    raise ValueError(f"filters.setups.{setup_id}.{key} must be >= 0.5")
                normalized_setup[key] = coerced
            normalized[str(setup_id)] = normalized_setup
        return normalized


class TrackingConfig(BaseModel):
    enabled: bool = True
    pending_expiry_minutes: int = Field(default=180, ge=15, le=1440)
    active_expiry_minutes: int = Field(default=240, ge=30, le=240)
    outcome_retention_days: int = Field(default=90, ge=7, le=3650)
    move_stop_to_break_even_on_tp1: bool = True
    min_stop_distance_pct: float = Field(default=0.5, ge=0.0, le=100.0)
    max_stop_distance_pct: float = Field(default=15.0, ge=0.5, le=100.0)
    agg_trade_page_limit: int = Field(default=6, ge=1, le=20)
    agg_trade_page_size: int = Field(default=1000, ge=100, le=1000)


_ALL_SETUP_IDS: tuple[str, ...] = (
    # Original 5
    "structure_pullback",
    "structure_break_retest",
    "wick_trap_reversal",
    "squeeze_setup",
    "ema_bounce",
    # New 10
    "fvg_setup",
    "order_block",
    "liquidity_sweep",
    "bos_choch",
    "hidden_divergence",
    "funding_reversal",
    "cvd_divergence",
    "session_killzone",
    "breaker_block",
    "turtle_soup",
    # Phase 5.3 expansion
    "vwap_trend",
    "supertrend_follow",
    "price_velocity",
    "volume_anomaly",
    "volume_climax_reversal",
    "keltner_breakout",
    # Roadmap expansion
    "whale_walls",
    "spread_strategy",
    "depth_imbalance",
    "absorption",
    "aggression_shift",
    "liquidation_heatmap",
    "stop_hunt_detection",
    "multi_tf_trend",
    "rsi_divergence_bottom",
    "wyckoff_spring",
    "bb_squeeze",
    "atr_expansion",
    "ls_ratio_extreme",
    "oi_divergence",
    "btc_correlation",
    "altcoin_season_index",
)


class SetupConfig(BaseModel):
    # Original 5 setups
    structure_pullback: bool = True
    structure_break_retest: bool = True
    wick_trap_reversal: bool = True
    squeeze_setup: bool = True
    ema_bounce: bool = True
    # New 10 setups (disabled by default until registered in SetupRegistry)
    fvg_setup: bool = True
    order_block: bool = True
    liquidity_sweep: bool = True
    bos_choch: bool = True
    hidden_divergence: bool = True
    funding_reversal: bool = True
    cvd_divergence: bool = True
    session_killzone: bool = True
    breaker_block: bool = True
    turtle_soup: bool = True
    # Phase 5.3 expansion
    vwap_trend: bool = True
    supertrend_follow: bool = True
    price_velocity: bool = True
    volume_anomaly: bool = True
    volume_climax_reversal: bool = True
    keltner_breakout: bool = True
    # Roadmap expansion
    whale_walls: bool = True
    spread_strategy: bool = True
    depth_imbalance: bool = True
    absorption: bool = True
    aggression_shift: bool = True
    liquidation_heatmap: bool = True
    stop_hunt_detection: bool = True
    multi_tf_trend: bool = True
    rsi_divergence_bottom: bool = True
    wyckoff_spring: bool = True
    bb_squeeze: bool = True
    atr_expansion: bool = True
    ls_ratio_extreme: bool = True
    oi_divergence: bool = True
    btc_correlation: bool = True
    altcoin_season_index: bool = True

    def enabled_setup_ids(self) -> tuple[str, ...]:
        enabled: list[str] = []
        for setup_id in _ALL_SETUP_IDS:
            if bool(getattr(self, setup_id, False)):
                enabled.append(setup_id)
        return tuple(enabled)


class ScoringConfig(BaseModel):
    """Weights for the simplified structure-based scoring engine."""

    enabled: bool = True
    setup_prior_weight: float = Field(default=0.65, ge=0.0, le=1.0)
    weight_mtf_alignment: float = Field(default=0.25, ge=0.0, le=1.0)
    weight_volume_quality: float = Field(default=0.20, ge=0.0, le=1.0)
    weight_structure_clarity: float = Field(default=0.20, ge=0.0, le=1.0)
    weight_risk_reward: float = Field(default=0.15, ge=0.0, le=1.0)
    weight_crowd_position: float = Field(default=0.10, ge=0.0, le=1.0)
    weight_oi_momentum: float = Field(default=0.10, ge=0.0, le=1.0)
    funding_rate_extreme: float = Field(default=0.0010, ge=0.0, le=0.02)
    funding_rate_moderate: float = Field(default=0.0005, ge=0.0, le=0.01)

    @model_validator(mode="after")
    def _validate_weights_not_effectively_disabled(self) -> "ScoringConfig":
        if not self.enabled:
            return self
        weights = {
            "weight_mtf_alignment": float(self.weight_mtf_alignment),
            "weight_volume_quality": float(self.weight_volume_quality),
            "weight_structure_clarity": float(self.weight_structure_clarity),
            "weight_risk_reward": float(self.weight_risk_reward),
            "weight_crowd_position": float(self.weight_crowd_position),
            "weight_oi_momentum": float(self.weight_oi_momentum),
        }
        total_weight = sum(weights.values())
        if total_weight <= 0.0:
            raise ValueError(
                "ScoringConfig: model component weights must have positive total"
            )
        if abs(total_weight - 1.0) > 1e-6:
            for key, value in weights.items():
                object.__setattr__(self, key, value / total_weight)
        return self


class AlertConfig(BaseModel):
    """Pre-alert funnel configuration.

    Designed for a signal-only bot (no auto-trading):
    - Watch alerts fire only when price enters an "interest zone"
    - Entry-zone alerts are optional and rate-limited
    - Invalidations are suppressed until the watch is old enough (anti-spam)
    """

    enabled: bool = Field(default=False)
    enable_entry_zone: bool = Field(default=True)

    # Zones are centered on the chosen structural level.
    watch_interest_pct: float = Field(default=0.015, ge=0.001, le=0.10)
    entry_zone_pct: float = Field(default=0.003, ge=0.0005, le=0.05)

    # Invalidation requires price to move beyond the interest zone by this buffer.
    invalidate_buffer_pct: float = Field(default=0.005, ge=0.0, le=0.10)

    # Anti-spam limits.
    max_watch_alerts_per_hour: int = Field(default=3, ge=0, le=1000)
    watch_symbol_cooldown_minutes: int = Field(default=60, ge=0, le=1440)
    entry_symbol_cooldown_minutes: int = Field(default=15, ge=0, le=1440)

    # Suppress invalidated alerts until the watch lives long enough.
    invalidated_min_age_minutes: int = Field(default=10, ge=0, le=240)

    # Hard expiry for a watch state (prevents stale alerts hours later).
    watch_expiry_minutes: int = Field(default=60, ge=5, le=1440)


class MLConfig(BaseModel):
    """Machine learning configuration for signal enhancement."""

    enabled: bool = False
    use_ml_in_live: bool = False
    ml_confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    model_dir: str = "models"
    model_type: str = "xgboost"  # xgboost, lightgbm, sklearn
    auto_retrain: bool = False
    retrain_interval_hours: int = Field(default=24, ge=1, le=168)


class NotifierWebhookConfig(BaseModel):
    enabled: bool = False
    webhook_url: str | None = None
    username: str | None = None
    bearer_token: str | None = None
    include_html: bool = True

    @field_validator("webhook_url", "username", "bearer_token")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


class NotifierConfig(BaseModel):
    provider: Literal["none", "telegram", "slack", "discord", "webhook"] = "telegram"
    slack: NotifierWebhookConfig = Field(default_factory=NotifierWebhookConfig)
    discord: NotifierWebhookConfig = Field(default_factory=NotifierWebhookConfig)
    webhook: NotifierWebhookConfig = Field(default_factory=NotifierWebhookConfig)

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        return str(value or "telegram").strip().lower()


class SpotCompanionConfig(BaseModel):
    enabled: bool = False
    lead_symbols: tuple[str, ...] = ()
    refresh_interval_seconds: int = Field(default=60, ge=5, le=3600)

    @field_validator("lead_symbols")
    @classmethod
    def _normalize_lead_symbols(
        cls, value: tuple[str, ...] | list[str] | None
    ) -> tuple[str, ...]:
        return tuple(
            str(item).strip().upper() for item in (value or ()) if str(item).strip()
        )


class IntelligenceConfig(BaseModel):
    """Public-only analytics, guardrails, and AI-agent telemetry."""

    enabled: bool = True
    runtime_mode: Literal["signal_only"] = "signal_only"
    source_policy: Literal["binance_only"] = "binance_only"
    smart_exit_mode: Literal["heuristic_v1"] = "heuristic_v1"
    gamma_semantics: Literal["proxy_only"] = "proxy_only"
    allow_runtime_options_eapi: bool = False
    refresh_interval_seconds: int = Field(default=900, ge=60, le=86400)
    write_hourly_reports: bool = True
    options_expiry_count: int = Field(default=2, ge=1, le=8)
    barrier_symbol_count: int = Field(default=2, ge=1, le=8)
    hard_barrier_pct: float = Field(default=1.5, ge=0.1, le=20.0)
    hard_barrier_window_minutes: int = Field(default=15, ge=5, le=240)
    smart_exit_enabled: bool = True
    smart_exit_threshold: float = Field(default=0.62, ge=0.0, le=1.0)
    regime_detector: Literal["legacy", "hmm", "gmm_var", "composite"] = "composite"
    max_consecutive_stop_losses: int = Field(default=3, ge=1, le=20)
    stop_loss_pause_hours: int = Field(default=5, ge=0, le=168)
    benchmark_symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    option_underlyings: tuple[str, ...] = ("BTC", "ETH")
    macro_symbols: tuple[str, ...] = ("^VIX", "DX-Y.NYB", "^TNX", "^GSPC")

    @field_validator("benchmark_symbols")
    @classmethod
    def _normalize_benchmark_symbols(
        cls, value: tuple[str, ...] | list[str] | None
    ) -> tuple[str, ...]:
        return tuple(
            str(item).strip().upper() for item in (value or ()) if str(item).strip()
        )

    @field_validator("option_underlyings")
    @classmethod
    def _normalize_option_underlyings(
        cls, value: tuple[str, ...] | list[str] | None
    ) -> tuple[str, ...]:
        return tuple(
            str(item).strip().upper() for item in (value or ()) if str(item).strip()
        )

    @field_validator("macro_symbols")
    @classmethod
    def _normalize_macro_symbols(
        cls, value: tuple[str, ...] | list[str] | None
    ) -> tuple[str, ...]:
        return tuple(str(item).strip() for item in (value or ()) if str(item).strip())


class WSConfig(BaseModel):
    """WebSocket configuration.

    Runtime policy:
    - `public` / `market` are logical endpoint classes used for subscription routing
    - routed Binance endpoints must stay split by stream class
    - aggTrade, when enabled, is restricted to tracked/active symbols only
    """

    enabled: bool = True
    base_url: str = "wss://fstream.binance.com"
    public_base_url: str = "wss://fstream.binance.com/public"
    market_base_url: str = "wss://fstream.binance.com/market"
    # 4h stays REST-authoritative; WS carries 5m/15m/1h live trigger/context data.
    kline_intervals: tuple[str, ...] = ("5m", "15m", "1h")
    subscription_scope: str = "shortlist"
    subscribe_book_ticker: bool = True
    subscribe_agg_trade: bool = True
    subscribe_chunk_size: int = Field(
        default=10, ge=5, le=200
    )  # Reduced for Binance limits
    subscribe_chunk_delay_ms: int = Field(
        default=500, ge=100, le=2000
    )  # Binance allows 10 incoming control messages/sec per connection.
    health_check_silence_seconds: float = Field(default=60.0, ge=10.0, le=300.0)
    market_reconnect_grace_seconds: float = Field(default=90.0, ge=60.0, le=120.0)
    agg_trade_window_seconds: int = Field(default=300, ge=10, le=3600)
    kline_cache_size: int = Field(default=300, ge=50, le=1000)
    warmup_timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0)
    reconnect_max_delay_seconds: float = Field(default=300.0, ge=1.0, le=3600.0)
    max_agg_trade_buffer: int = Field(default=10000, ge=100, le=100000)
    rest_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    backfill_failure_cooldown_seconds: int = Field(default=900, ge=0, le=86400)
    # Global market streams (no per-symbol subscription needed)
    subscribe_market_streams: bool = True
    market_ticker_freshness_seconds: float = Field(default=120.0, ge=10.0, le=600.0)
    force_order_window_seconds: int = Field(default=60, ge=10, le=3600)
    # Intra-candle analysis throttle (seconds between analysis per symbol)
    # Lower = more real-time, higher = less CPU load
    intra_candle_throttle_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    # Optional noise gate: minimum mid-price move (in bps) required to trigger
    # another intra-candle scan for the same symbol.
    intra_candle_min_move_bps: float = Field(default=0.0, ge=0.0, le=100.0)

    @field_validator("base_url", "public_base_url", "market_base_url")
    @classmethod
    def _normalize_ws_base_url(cls, value: str) -> str:
        raw = str(value or "wss://fstream.binance.com").strip().rstrip("/")
        if raw.endswith("/stream"):
            raw = raw.removesuffix("/stream")
        if raw.endswith("/ws"):
            raw = raw.removesuffix("/ws")
        return raw

    @field_validator("subscription_scope")
    @classmethod
    def _normalize_subscription_scope(cls, value: str) -> str:
        raw = str(value or "shortlist").strip().lower()
        if raw not in {"tracked_only", "shortlist"}:
            raise ValueError(
                "ws.subscription_scope must be one of: tracked_only, shortlist"
            )
        return raw

    @field_validator("kline_intervals")
    @classmethod
    def _normalize_intervals(
        cls, value: tuple[str, ...] | list[str] | None
    ) -> tuple[str, ...]:
        return tuple(str(v).strip() for v in (value or ()) if str(v).strip())

    @model_validator(mode="after")
    def _resolve_endpoint_urls(self) -> "WSConfig":
        normalized_root = (
            str(self.base_url or "wss://fstream.binance.com").strip().rstrip("/")
        )
        for suffix in ("/public", "/market"):
            if normalized_root.endswith(suffix):
                normalized_root = normalized_root[: -len(suffix)]
                break
        self.base_url = normalized_root

        def routed_endpoint(field_name: str, suffix: str) -> str:
            value = str(getattr(self, field_name) or "").strip().rstrip("/")
            if not value:
                return f"{normalized_root}{suffix}"
            if value.endswith("/stream"):
                value = value.removesuffix("/stream")
            if value.endswith("/ws"):
                value = value.removesuffix("/ws")
            if value.endswith(suffix):
                return value
            for other_suffix in ("/public", "/market"):
                if value.endswith(other_suffix):
                    value = value[: -len(other_suffix)]
                    break
            return f"{value}{suffix}"

        self.public_base_url = routed_endpoint("public_base_url", "/public")
        self.market_base_url = routed_endpoint("market_base_url", "/market")
        return self

    def endpoint_base_url(self, endpoint_class: str) -> str:
        if endpoint_class == "public":
            return self.public_base_url
        if endpoint_class == "market":
            return self.market_base_url
        raise ValueError(f"unsupported ws endpoint_class={endpoint_class}")


class BotSettings(BaseModel):
    tg_token: str
    target_chat_id: str
    data_dir: Path = Path("data") / "bot"
    config_path: Path = Path("config.toml")
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    setups: SetupConfig = Field(default_factory=SetupConfig)
    ws: WSConfig = Field(default_factory=WSConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    ml: MLConfig = Field(default_factory=MLConfig)
    notifiers: NotifierConfig = Field(default_factory=NotifierConfig)
    spot_companion: SpotCompanionConfig = Field(default_factory=SpotCompanionConfig)
    intelligence: IntelligenceConfig = Field(default_factory=IntelligenceConfig)
    assets: dict[str, AssetConfig] = Field(default_factory=dict)

    @property
    def telemetry_dir(self) -> Path:
        return self.data_dir / self.runtime.telemetry_subdir

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def features_store_file(self) -> Path:
        return self.data_dir / "features_store.json"

    @property
    def pid_file(self) -> Path:
        return self.data_dir / "bot.pid"

    @property
    def ml_dir(self) -> Path:
        return self.data_dir / "ml"

    @property
    def log_level(self) -> str:
        return self.runtime.log_level

    @field_validator("tg_token")
    @classmethod
    def _validate_tg_token(cls, value: str) -> str:
        token = str(value or "").strip()
        # Telegram tokens format: 123456789:ABCdefGHIjklMNOpqrsTUVwxyZ
        # Allow alphanumerics, underscore, hyphen, and colon
        if token:
            allowed_chars = set(
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:-"
            )
            if not all(c in allowed_chars for c in token):
                raise ValueError("tg_token contains invalid characters")
        return token

    @field_validator("target_chat_id")
    @classmethod
    def _validate_target_chat_id(cls, value: str) -> str:
        chat_id = str(value or "").strip()
        if chat_id:
            # Can be numeric ID or channel username (starts with @)
            if chat_id.startswith("@"):
                if len(chat_id) < 2:
                    raise ValueError("target_chat_id channel username too short")
            elif not chat_id.lstrip("-").isdigit():
                raise ValueError("target_chat_id must be numeric ID or @channel")
        return chat_id

    @model_validator(mode="after")
    def _validate_timing_coherence(self) -> "BotSettings":
        cooldown = self.filters.cooldown_minutes
        pending = self.tracking.pending_expiry_minutes
        if cooldown > pending:
            raise ValueError(
                f"cooldown_minutes ({cooldown}) must be <= "
                f"pending_expiry_minutes ({pending})"
            )
        return self

    @model_validator(mode="after")
    def _validate_kline_intervals(self) -> "BotSettings":
        """Validate kline intervals are supported by Binance."""
        valid_intervals = {
            "1m",
            "3m",
            "5m",
            "15m",
            "30m",
            "1h",
            "2h",
            "4h",
            "6h",
            "8h",
            "12h",
            "1d",
            "3d",
            "1w",
            "1M",
        }
        intervals = cast(list[str], self.ws.kline_intervals)
        for interval in intervals:
            if interval not in valid_intervals:
                raise ValueError(
                    f"Invalid kline interval: {interval}. Valid: {valid_intervals}"
                )
        return self

    @model_validator(mode="after")
    def _normalize_asset_overrides(self) -> "BotSettings":
        self.assets = {
            str(symbol).strip().upper(): config
            for symbol, config in self.assets.items()
        }
        return self

    def validate_for_runtime(self, *, require_telegram: bool) -> None:
        """Validate settings for runtime execution."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        # Check data_dir is writable
        test_file = self.data_dir / ".write_test"
        try:
            test_file.write_text("test")
            test_file.unlink()
        except Exception as exc:
            raise ValueError(f"data_dir is not writable: {self.data_dir} ({exc})")

        ws_urls = {
            "ws.base_url": self.ws.base_url,
            "ws.public_base_url": self.ws.public_base_url,
            "ws.market_base_url": self.ws.market_base_url,
        }
        forbidden_tokens = ("/private", "listenkey", "/ws-api", "/sapi", "/papi")
        for label, url in ws_urls.items():
            lowered = str(url or "").strip().lower()
            if any(token in lowered for token in forbidden_tokens):
                raise ValueError(
                    f"{label} must point to Binance public market streams only: {url}"
                )
        if (
            str(self.ws.public_base_url).rstrip("/").lower().endswith("/public")
            is False
        ):
            raise ValueError(
                f"ws.public_base_url must use Binance /public routed endpoint: {self.ws.public_base_url}"
            )
        if (
            str(self.ws.market_base_url).rstrip("/").lower().endswith("/market")
            is False
        ):
            raise ValueError(
                f"ws.market_base_url must use Binance /market routed endpoint: {self.ws.market_base_url}"
            )

        if require_telegram:
            if not self.tg_token.strip():
                raise ValueError("TG_TOKEN is required for runtime (set in .env)")
            if not self.target_chat_id.strip():
                raise ValueError("TARGET_CHAT_ID is required for runtime (set in .env)")
            if len(self.tg_token) < 20:
                raise ValueError("TG_TOKEN looks too short (expected ~46 chars)")


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        parsed = _toml_lib.load(handle)
    return parsed if isinstance(parsed, dict) else {}


def _resolve_config_source(config_file: Path) -> Path:
    if config_file.exists():
        return config_file
    if config_file.name == "config.toml":
        legacy_alias = config_file.with_name("config..toml")
        if legacy_alias.exists():
            return legacy_alias
        example_path = config_file.with_name("config.toml.example")
        if example_path.exists():
            return example_path
    return config_file


def _flatten_legacy_strategy_config(config: Mapping[str, Any]) -> dict[str, float]:
    """Flatten nested legacy strategy config into flat numeric overrides."""
    flattened: dict[str, float] = {}
    stack: list[Mapping[str, Any]] = [config]
    while stack:
        current = stack.pop()
        for key, value in current.items():
            if isinstance(value, Mapping):
                stack.append(value)
                continue
            if isinstance(value, bool):
                flattened[str(key)] = float(value)
                continue
            if isinstance(value, (int, float)):
                flattened[str(key)] = float(value)
    return flattened


def _load_legacy_strategy_overrides(config_root: Path) -> dict[str, dict[str, float]]:
    """Load config/strategies/*.toml once and map to filters.setups format."""
    overrides: dict[str, dict[str, float]] = {}
    legacy_dir = config_root / "config" / "strategies"
    if not legacy_dir.exists():
        return overrides
    for file_path in sorted(legacy_dir.glob("*.toml")):
        parsed = _load_toml(file_path)
        if not parsed:
            continue
        setup_id = file_path.stem
        overrides[setup_id] = _flatten_legacy_strategy_config(parsed)
    return overrides


def _convert_toml_dict(d: dict[Any, Any]) -> dict[str, Any]:
    """Convert TOML dict with possible bytes keys to string keys."""
    result: dict[str, Any] = {}
    for k, v in d.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        if isinstance(v, dict):
            result[key] = _convert_toml_dict(v)
        elif isinstance(v, list):
            result[key] = [
                _convert_toml_dict(i) if isinstance(i, dict) else i for i in v
            ]
        else:
            result[key] = v
    return result


def load_settings(config_path: str | Path = "config.toml") -> BotSettings:
    config_file = Path(config_path)
    resolved_config = _resolve_config_source(config_file)
    parsed = _load_toml(resolved_config)
    bot_raw = parsed.get("bot") if isinstance(parsed.get("bot"), dict) else {}
    payload = _convert_toml_dict(cast(dict[Any, Any], bot_raw))
    secrets = load_secrets()
    payload["tg_token"] = secrets.tg_token
    payload["target_chat_id"] = secrets.target_chat_id
    payload["config_path"] = resolved_config
    notifiers_payload = payload.setdefault("notifiers", {})
    if isinstance(notifiers_payload, dict):
        provider_override = str(os.getenv("BOT_NOTIFIER_PROVIDER", "") or "").strip().lower()
        provider = str(
            notifiers_payload.get("provider", "telegram") or "telegram"
        ).strip().lower()
        if provider_override:
            notifiers_payload["provider"] = provider_override
        elif provider == "none" and secrets.tg_token and secrets.target_chat_id:
            notifiers_payload["provider"] = "telegram"
    payload.setdefault("data_dir", Path("data") / "bot")
    filters_payload = payload.setdefault("filters", {})
    if not isinstance(filters_payload, dict):
        filters_payload = {}
        payload["filters"] = filters_payload
    setup_overrides = filters_payload.setdefault("setups", {})
    if not isinstance(setup_overrides, dict):
        setup_overrides = {}
        filters_payload["setups"] = setup_overrides
    legacy_overrides = _load_legacy_strategy_overrides(config_file.parent)
    for setup_id, legacy_params in legacy_overrides.items():
        existing = setup_overrides.get(setup_id)
        if isinstance(existing, dict):
            setup_overrides[setup_id] = {**legacy_params, **existing}
        else:
            setup_overrides[setup_id] = dict(legacy_params)
    settings = BotSettings.model_validate(payload)
    return settings

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

LOG = logging.getLogger(__name__)

from engine.contract import (
    DEFAULT_SCALE_WEIGHTS,
    default_ttl_bars,
    normalize_scale_weights,
    resolve_target_rr,
    valid_until_from,
    validate_signal_contract,
)

if TYPE_CHECKING:
    import polars as pl

    from .config import BotSettings

SIGNAL_CONTRACT_VIOLATION_PREFIX = "Signal contract violations"


def is_signal_contract_violation(exc: BaseException) -> bool:
    """True when Signal.__post_init__ rejected an invalid trade plan."""
    return isinstance(exc, ValueError) and str(exc).startswith(SIGNAL_CONTRACT_VIOLATION_PREFIX)


@dataclass(frozen=True, slots=True)
class SymbolMeta:
    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    status: str
    onboard_date_ms: int


@dataclass(frozen=True, slots=True)
class UniverseSymbol:
    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    status: str
    onboard_date_ms: int
    quote_volume: float
    price_change_pct: float
    last_price: float
    trade_count_24h: int | None = None
    shortlist_bucket: str = ""
    shortlist_score: float | None = None
    shortlist_reasons: tuple[str, ...] = ()
    seed_source: str = "unknown"
    liquidity_rank: int | None = None
    strategy_fits: tuple[str, ...] = ()


@dataclass(slots=True)
class SymbolFrames:
    symbol: str
    df_1h: pl.DataFrame
    df_15m: pl.DataFrame
    bid_price: float | None
    ask_price: float | None
    df_5m: pl.DataFrame | None = None
    df_4h: pl.DataFrame | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None
    frame_source_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AggTradeSnapshot:
    symbol: str
    trade_count: int
    buy_qty: float
    sell_qty: float
    delta_ratio: float | None


@dataclass(frozen=True, slots=True)
class AggTrade:
    symbol: str
    trade_id: int
    price: float
    quantity: float
    trade_time_ms: int
    is_buyer_maker: bool

    @property
    def trade_time(self) -> datetime:
        return datetime.fromtimestamp(self.trade_time_ms / 1000.0, tz=UTC)


@dataclass(slots=True)
class PreparedSymbol:
    universe: UniverseSymbol
    work_1h: pl.DataFrame
    work_15m: pl.DataFrame
    bid_price: float | None
    ask_price: float | None
    spread_bps: float | None
    work_5m: pl.DataFrame | None = None
    work_4h: pl.DataFrame | None = None
    work_primary: pl.DataFrame | None = None
    bias_4h: str = "neutral"  # 4H macro context (market regime)
    bias_1h: str = "neutral"  # 1H trading context for 15M signals
    # Optional fields populated from WS global streams (mark price / liquidations)
    mark_price: float | None = None
    ticker_price: float | None = None
    funding_rate: float | None = None
    funding_recent_extreme_rate: float | None = None
    funding_recent_extreme_age_hours: float | None = None
    oi_current: float | None = None
    oi_change_pct: float | None = None
    ls_ratio: float | None = None
    top_account_ls_ratio: float | None = None
    taker_ratio: float | None = None  # taker buy/sell volume ratio (>1.0 = net buyers)
    liquidation_score: float | None = None  # -1.0 (bearish liq) … +1.0 (bullish liq)
    liquidation_cascade_5m: bool | None = None
    funding_rate_zscore_48h: float | None = None
    funding_trend: str | None = None  # "rising" | "falling" | "flat" | None
    estimated_settle_price: float | None = None
    interest_rate: float | None = None
    next_funding_time_ms: int | None = None
    funding_rate_cap: float | None = None
    funding_rate_floor: float | None = None
    funding_interval_hours: int | None = None
    basis_pct: float | None = (
        None  # (futures - index) / index * 100; + = contango, - = backwardation
    )
    global_ls_ratio: float | None = None
    global_account_ls_ratio: float | None = None
    top_trader_position_ratio: float | None = None
    top_position_ls_ratio: float | None = None
    top_vs_global_ls_gap: float | None = None
    mark_index_spread_bps: float | None = None
    premium_zscore_5m: float | None = None
    premium_slope_5m: float | None = None
    oi_slope_5m: float | None = None
    depth_imbalance: float | None = None
    microprice_bias: float | None = None
    depth_wall_pressure: float | None = None
    depth_imbalance_source: str | None = None
    microprice_bias_source: str | None = None
    depth_book_age_seconds: float | None = None
    agg_trade_delta_30s: float | None = None
    aggression_shift: float | None = None
    orderflow_source: str | None = None
    liquidation_score_source: str | None = None
    liquidation_score_age_seconds: float | None = None
    spot_lead_return_1m: float | None = None
    spot_futures_spread_bps: float | None = None
    btc_bias: str | None = None
    eth_bias: str | None = None
    sol_bias: str | None = None
    xau_bias: str | None = None
    xag_bias: str | None = None
    pax_bias: str | None = None
    altcoin_season_index: float | None = None
    btc_phase: str | None = None
    global_market_regime: str | None = None
    macro_risk_mode: str | None = None
    benchmark_context: dict[str, Any] = field(default_factory=dict)
    market_ctx: dict[str, Any] = field(default_factory=dict)
    market_context_age_seconds: float | None = None
    mark_price_age_seconds: float | None = None
    ticker_price_age_seconds: float | None = None
    book_ticker_age_seconds: float | None = None
    context_snapshot_age_seconds: float | None = None
    data_freshness_flags: tuple[str, ...] = ()
    data_quality_flags: list[str] = field(default_factory=list)
    data_source_mix: str = "futures_only"
    degraded: bool = False
    degrade_reason: str | None = None
    fallback_used: str | None = None
    market_regime: str = "neutral"  # "trending" | "neutral" | "choppy"
    # Structure-based fields (Фаза 2 рефакторинга)
    structure_1h: str = "ranging"  # "uptrend" | "downtrend" | "ranging"
    regime_4h_confirmed: str = (
        "ranging"  # "uptrend" | "downtrend" | "ranging" (3+ bars) - macro only
    )
    regime_1h_confirmed: str = (
        "ranging"  # "uptrend" | "downtrend" | "ranging" (3+ bars) - trading context
    )
    poc_1h: float | None = None  # Point of Control on 1h (highest volume price)
    poc_15m: float | None = None  # Point of Control on 15m
    vah_1h: float | None = None
    val_1h: float | None = None
    vah_15m: float | None = None
    val_15m: float | None = None
    primary_timeframe: str = "15m"
    context_timeframes: tuple[str, ...] = ("1h", "4h")
    settings: BotSettings | None = None
    reject_log: tuple[dict[str, Any], ...] = ()
    btc_change_pct: float | None = None
    eth_change_pct: float | None = None
    btc_corr_1h: float | None = None

    def __post_init__(self) -> None:
        if self.top_account_ls_ratio is None and self.ls_ratio is not None:
            self.top_account_ls_ratio = self.ls_ratio
        if self.ls_ratio is None and self.top_account_ls_ratio is not None:
            self.ls_ratio = self.top_account_ls_ratio
        if self.global_account_ls_ratio is None and self.global_ls_ratio is not None:
            self.global_account_ls_ratio = self.global_ls_ratio
        if self.global_ls_ratio is None and self.global_account_ls_ratio is not None:
            self.global_ls_ratio = self.global_account_ls_ratio
        if self.top_position_ls_ratio is None and self.top_trader_position_ratio is not None:
            self.top_position_ls_ratio = self.top_trader_position_ratio
        if self.top_trader_position_ratio is None and self.top_position_ls_ratio is not None:
            self.top_trader_position_ratio = self.top_position_ls_ratio
        if not isinstance(self.reject_log, tuple):
            self.reject_log = tuple(self.reject_log)
        if self.work_primary is None:
            if self.primary_timeframe == "5m" and self.work_5m is not None:
                self.work_primary = self.work_5m
            elif self.primary_timeframe == "1h":
                self.work_primary = self.work_1h
            elif self.primary_timeframe == "4h" and self.work_4h is not None:
                self.work_primary = self.work_4h
            else:
                self.work_primary = self.work_15m

    @property
    def symbol(self) -> str:
        return self.universe.symbol

    @property
    def atr_pct(self) -> float | None:
        if self.work_15m.is_empty() or "atr_pct" not in self.work_15m.columns:
            return None
        value = self.work_15m.item(-1, "atr_pct")
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    @property
    def volume_ratio(self) -> float | None:
        if self.work_15m.is_empty() or "volume_ratio20" not in self.work_15m.columns:
            return None
        value = self.work_15m.item(-1, "volume_ratio20")
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    @property
    def adx_1h(self) -> float | None:
        if self.work_1h.is_empty() or "adx14" not in self.work_1h.columns:
            return None
        value = self.work_1h.item(-1, "adx14")
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    setup_id: str
    direction: str
    score: float
    timeframe: str
    entry_low: float
    entry_high: float
    stop: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float | None = None
    valid_until: datetime | None = None
    scale_weights: tuple[float, float, float] = DEFAULT_SCALE_WEIGHTS
    ttl_bars: int | None = None
    entry_plan_status: str = "valid"
    reasons: tuple[str, ...] = ()
    bias_4h: str = "neutral"
    quote_volume: float | None = None
    spread_bps: float | None = None
    atr_pct: float | None = None
    orderflow_delta_ratio: float | None = None
    oi_change_pct: float | None = None
    funding_rate: float | None = None
    strategy_family: str = "continuation"
    confirmation_profile: str = "trend_follow"
    entry_order_type: str = "limit"
    target_integrity_status: str | None = None
    single_target_mode: bool = False
    passed_filters: tuple[str, ...] = ()
    mark_price: float | None = None
    volume_ratio: float | None = None  # current volume / 20-bar avg (for analytics companion)
    adx_1h: float | None = None
    risk_reward: float | None = None
    trend_direction: str | None = None
    trend_score: float | None = None
    premium_zscore_5m: float | None = None
    premium_slope_5m: float | None = None
    ls_ratio: float | None = None
    microstructure_bias_score: float | None = None
    microstructure_confidence: float | None = None
    microstructure_label: str | None = None
    microstructure_reason: str | None = None
    microstructure_warnings: tuple[str, ...] = ()
    btc_bias: str | None = None
    eth_bias: str | None = None
    confirmation_count: int | None = None
    sol_bias: str | None = None
    xau_bias: str | None = None
    xag_bias: str | None = None
    pax_bias: str | None = None
    entry_tf: str = ""
    pattern_tf: str = ""
    context_tfs: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
            object.__setattr__(self, "created_at", created_at)
        else:
            created_at = created_at.astimezone(UTC)
            object.__setattr__(self, "created_at", created_at)
        scale_weights = normalize_scale_weights(self.scale_weights)
        object.__setattr__(self, "scale_weights", scale_weights)

        if self.valid_until is None:
            ttl_bars = (
                int(self.ttl_bars)
                if self.ttl_bars is not None
                else default_ttl_bars(self.setup_id, self.strategy_family, self.timeframe)
            )
            object.__setattr__(
                self,
                "valid_until",
                valid_until_from(
                    created_at=created_at,
                    setup_id=self.setup_id,
                    strategy_family=self.strategy_family,
                    timeframe=self.timeframe,
                    ttl_bars=ttl_bars,
                ),
            )
            object.__setattr__(self, "ttl_bars", max(1, min(ttl_bars, 96)))
        elif self.valid_until.tzinfo is None:
            object.__setattr__(self, "valid_until", self.valid_until.replace(tzinfo=UTC))
        else:
            object.__setattr__(self, "valid_until", self.valid_until.astimezone(UTC))

        if self.take_profit_3 is None or not math.isfinite(float(self.take_profit_3)):
            risk = abs(self.entry_mid - self.stop)
            rr3 = resolve_target_rr(None)[2]
            if self.direction == "long":
                tp3 = self.entry_mid + risk * rr3
            else:
                tp3 = self.entry_mid - risk * rr3
            object.__setattr__(self, "take_profit_3", tp3)

        if self.risk_reward is None:
            risk = abs(self.entry_mid - self.stop)
            reward = abs(self.take_profit_1 - self.entry_mid)
            try:
                computed = (reward / risk) if risk > 0 else 0.0
            except ZeroDivisionError:
                computed = 0.0
            object.__setattr__(self, "risk_reward", computed)
        issues = validate_signal_contract(self)
        if issues:
            # build_trade_plan (contract.py) now owns the stop clamp — violations
            # here mean a detector bypassed build_trade_plan entirely.  Log a
            # warning so it surfaces without killing the analysis cycle; delivery
            # path will reject on its own contract check if the signal is broken.
            detail = [f"{issue.field}:{issue.reason}" for issue in issues]
            LOG.warning(
                "signal_contract_issues | setup=%s symbol=%s issues=%s",
                getattr(self, "setup_id", "?"),
                getattr(self, "symbol", "?"),
                detail,
            )

    @property
    def entry_mid_raw(self) -> float:
        return (self.entry_low + self.entry_high) / 2.0

    @property
    def entry_mid(self) -> float:
        return self.entry_mid_raw

    @property
    def entry_reference_price(self) -> float:
        mid = self.entry_mid_raw
        if (
            self.mark_price
            and self.mark_price > 0
            and mid > 0
            and abs(mid - self.mark_price) / mid < 0.002
        ):
            return self.mark_price
        return mid

    @property
    def stop_distance_pct(self) -> float:
        try:
            if self.entry_mid <= 0:
                return 0.0
            return abs(self.entry_mid - self.stop) / self.entry_mid * 100.0
        except ZeroDivisionError:
            return 0.0

    @property
    def signal_key(self) -> str:
        return f"{self.symbol}|{self.setup_id}|{self.direction}"

    @property
    def tracking_id(self) -> str:
        stamp = self.created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{self.signal_key}|{stamp}"

    @property
    def tracking_ref(self) -> str:
        digest = hashlib.sha1(self.tracking_id.encode("utf-8"), usedforsecurity=False).hexdigest()
        return digest[:8].upper()

    @property
    def content_hash(self) -> str:
        """Deterministic hash of symbol + direction + setup + rounded prices.

        Used for dedup (IV.27): signals with identical content_hash within a
        dedup window are treated as duplicates.
        """
        rounded_low = f"{self.entry_low:.2f}"
        rounded_high = f"{self.entry_high:.2f}"
        raw = (
            f"{self.symbol}|{self.direction}|{self.setup_id}|"
            f"{rounded_low}|{rounded_high}|{self.timeframe}"
        )
        return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]

    @property
    def side(self) -> str:
        return self.direction

    @property
    def entry(self) -> float:
        return self.entry_mid

    @property
    def sl(self) -> float:
        return self.stop

    @property
    def tp1(self) -> float:
        return self.take_profit_1

    @property
    def tp2(self) -> float:
        return self.take_profit_2

    @property
    def tp3(self) -> float:
        return float(self.take_profit_3 or 0.0)

    @property
    def stop_loss(self) -> float:
        return self.stop

    @property
    def entry_zone(self) -> tuple[float, float]:
        return (self.entry_low, self.entry_high)

    @property
    def valid_until_iso(self) -> str:
        return self.valid_until.isoformat() if self.valid_until is not None else ""

    @property
    def scale_weight_pct(self) -> tuple[int, int, int]:
        # type: ignore[return-value]
        return tuple(round(weight * 100) for weight in self.scale_weights)

    @property
    def time_to_expiry_minutes(self) -> float:
        if self.valid_until is None:
            return 0.0
        return max(0.0, (self.valid_until - datetime.now(UTC)).total_seconds() / 60.0)

    @property
    def target_count(self) -> int:
        return 1 if self.single_target_mode else 3

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "tracking_id": self.tracking_id,
            "tracking_ref": self.tracking_ref,
            "signal_key": self.signal_key,
            "timeframe": self.timeframe,
            "entry_tf": self.entry_tf or self.timeframe,
            "entry_tf_used": self.entry_tf or self.timeframe,
            "pattern_tf": self.pattern_tf,
            "context_tfs": self.context_tfs,
            "strategy_family": self.strategy_family,
            "confirmation_profile": self.confirmation_profile,
            "confirmation_count": self.confirmation_count,
            "entry_order_type": self.entry_order_type,
            "target_integrity_status": self.target_integrity_status,
            "single_target_mode": self.single_target_mode,
            "entry_plan_status": self.entry_plan_status,
            "valid_until": self.valid_until_iso,
            "ttl_bars": self.ttl_bars,
            "scale_weights": self.scale_weights,
        }

    def same_target(self, tolerance: float | None = None) -> bool:
        tol = tolerance
        if tol is None:
            anchor = max(
                abs(self.entry_mid),
                abs(self.take_profit_1),
                abs(self.take_profit_2),
                abs(self.tp3),
                1.0,
            )
            tol = anchor * 1e-8
        return math.isclose(self.take_profit_1, self.take_profit_2, abs_tol=tol, rel_tol=0.0)

    def to_log_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "setup_id": self.setup_id,
            "direction": self.direction,
            "score": round(self.score, 4),
            "timeframe": self.timeframe,
            "entry_low": round(self.entry_low, 8),
            "entry_high": round(self.entry_high, 8),
            "entry_zone": [round(self.entry_low, 8), round(self.entry_high, 8)],
            "entry_mid": round(self.entry_mid, 8),
            "entry_mid_raw": round(self.entry_mid_raw, 8),
            "entry_reference_price": round(self.entry_reference_price, 8),
            "stop": round(self.stop, 8),
            "stop_price": round(self.stop, 8),
            "stop_loss": round(self.stop_loss, 8),
            "take_profit_1": round(self.take_profit_1, 8),
            "take_profit_2": round(self.take_profit_2, 8),
            "take_profit_3": round(self.tp3, 8),
            "tp1_price": round(self.take_profit_1, 8),
            "tp2_price": round(self.take_profit_2, 8),
            "tp3_price": round(self.tp3, 8),
            "tp1": round(self.tp1, 8),
            "tp2": round(self.tp2, 8),
            "tp3": round(self.tp3, 8),
            "risk_reward": round(float(self.risk_reward or 0.0), 4),
            "stop_distance_pct": round(self.stop_distance_pct, 4),
            "valid_until": self.valid_until_iso,
            "ttl_bars": self.ttl_bars,
            "scale_weights": list(self.scale_weights),
            "bias_4h": self.bias_4h,
            "quote_volume": self.quote_volume,
            "spread_bps": self.spread_bps,
            "atr_pct": self.atr_pct,
            "orderflow_delta_ratio": self.orderflow_delta_ratio,
            "oi_change_pct": self.oi_change_pct,
            "funding_rate": self.funding_rate,
            "mark_price": self.mark_price,
            "volume_ratio": self.volume_ratio,
            "adx_1h": self.adx_1h,
            "premium_zscore_5m": self.premium_zscore_5m,
            "premium_slope_5m": self.premium_slope_5m,
            "ls_ratio": self.ls_ratio,
            "microstructure_bias_score": self.microstructure_bias_score,
            "microstructure_confidence": self.microstructure_confidence,
            "microstructure_label": self.microstructure_label,
            "microstructure_reason": self.microstructure_reason,
            "microstructure_warnings": list(self.microstructure_warnings),
            "btc_bias": self.btc_bias,
            "eth_bias": self.eth_bias,
            "sol_bias": self.sol_bias,
            "xau_bias": self.xau_bias,
            "xag_bias": self.xag_bias,
            "pax_bias": self.pax_bias,
            "strategy_family": self.strategy_family,
            "confirmation_profile": self.confirmation_profile,
            "entry_order_type": self.entry_order_type,
            "target_integrity_status": self.target_integrity_status,
            "single_target_mode": self.single_target_mode,
            "passed_filters": list(self.passed_filters),
            "reasons": list(self.reasons),
            "created_at": self.created_at.isoformat(),
            "tracking_id": self.tracking_id,
            "tracking_ref": self.tracking_ref,
        }


@dataclass(slots=True)
class PipelineResult:
    """Result container for signal analysis pipeline.

    Replaces legacy PipelineResult from pipeline.py for backward compatibility.
    Modern engine uses SignalResult in core/engine/base.py.
    """

    symbol: str
    trigger: str
    event_ts: datetime
    raw_setups: int
    candidates: list[Signal] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    delivered: list[Signal] = field(default_factory=list)
    error: str | None = None
    status: str | None = None
    prepared: PreparedSymbol | None = None
    funnel: dict[str, Any] = field(default_factory=dict)

"""Signal filtering pipeline."""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bot.runtime.errors import DEFENSIVE_EXC

from ..diagnostics.signals import get_global_diagnostics
from ..domain.mtf import evaluate_mtf_gate, normalize_mtf_reject_reason
from ..features.microstructure import MicrostructureContext, build_microstructure_context
from ..runtime_policy import configured_primary_timeframe, is_deep_analysis_symbol
from .contract import DEFAULT_TARGET_RR
from .filter_stages import filter_stage_enabled
from .scoring import ScoringResult
from .trade_plan import TradePlanBuilder

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal
    from .confluence import ConfluenceEngine


LOGGER = logging.getLogger(__name__)


_ADX_POLICY_HARD_GATE = "hard_gate"
_ADX_POLICY_PENALTY = "score_penalty"

# setup_id -> ADX policy override (highest precedence)
_ADX_POLICY_BY_SETUP: dict[str, str] = {
    "wick_trap_reversal": _ADX_POLICY_PENALTY,
    "funding_reversal": _ADX_POLICY_PENALTY,
    "turtle_soup": _ADX_POLICY_PENALTY,
}

# strategy_family -> ADX policy fallback
_ADX_POLICY_BY_FAMILY: dict[str, str] = {
    "reversal": _ADX_POLICY_PENALTY,
    "trend_follow": _ADX_POLICY_HARD_GATE,
    "continuation": _ADX_POLICY_HARD_GATE,
}

_TREND_CONFLICT_SOFT_FAMILIES = {
    "reversal",
    "orderflow",
    "liquidity",
    "sentiment",
}
_TREND_CONFLICT_SOFT_PROFILES = {
    "countertrend_exhaustion",
    "divergence_reversal",
}


def _resolve_adx_policy(signal: Signal) -> str:
    setup_policy = _ADX_POLICY_BY_SETUP.get(signal.setup_id)
    if setup_policy:
        return setup_policy
    family_policy = _ADX_POLICY_BY_FAMILY.get(signal.strategy_family)
    if family_policy:
        return family_policy
    if signal.confirmation_profile == "trend_follow":
        return _ADX_POLICY_HARD_GATE
    return _ADX_POLICY_PENALTY


def _uses_soft_trend_conflict(signal: Signal) -> bool:
    """Countertrend strategies should be penalized, not globally suppressed."""
    return (
        signal.strategy_family in _TREND_CONFLICT_SOFT_FAMILIES
        or signal.confirmation_profile in _TREND_CONFLICT_SOFT_PROFILES
    )


def _benchmark_context_guard(
    signal: Signal,
    prepared: PreparedSymbol,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Reject thin alt entries that fight dominant benchmark pressure.

    BTC, ETH and SOL often drag the rest of the USD-M universe. XAU/XAG are
    tracked as macro/risk proxies but are not enough by themselves to block a
    crypto setup. Missing context stays non-blocking and is surfaced in details.
    """
    symbol = str(getattr(prepared, "symbol", "") or "").upper()
    context = getattr(prepared, "benchmark_context", {}) or {}
    if not isinstance(context, dict) or symbol in {"BTCUSDT", "ETHUSDT", "SOLUSDT"}:
        return True, None, {"benchmark_context_available": bool(context)}

    crypto_symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    pct_1h_values: list[float | None] = []
    for benchmark in crypto_symbols:
        payload = context.get(benchmark)
        if not isinstance(payload, dict):
            pct_1h_values.append(None)
            continue
        raw_pct = payload.get("pct_1h")
        if raw_pct is None:
            pct_1h_values.append(None)
            continue
        try:
            pct_1h_values.append(float(raw_pct))
        except TypeError, ValueError:
            pct_1h_values.append(None)
    context_age_seconds = getattr(prepared, "context_snapshot_age_seconds", None)
    try:
        context_age = float(context_age_seconds) if context_age_seconds is not None else None
    except TypeError, ValueError:
        context_age = None
    benchmark_context_stale = bool(
        context
        and context_age is not None
        and context_age > 600.0
        and all(value is None or value == 0.0 for value in pct_1h_values)
    )
    if benchmark_context_stale:
        LOGGER.info(
            "benchmark context stale | symbol=%s age_seconds=%.1f pct_1h_values=%s",
            symbol,
            context_age,
            pct_1h_values,
        )

    votes: list[dict[str, Any]] = []
    for benchmark in crypto_symbols:
        payload = context.get(benchmark)
        if not isinstance(payload, dict):
            continue
        bias = str(payload.get("bias") or "neutral").lower()
        try:
            pct_1h = float(payload.get("pct_1h") or 0.0)
        except TypeError, ValueError:
            pct_1h = 0.0
        try:
            pct_4h = float(payload.get("pct_4h") or 0.0)
        except TypeError, ValueError:
            pct_4h = 0.0
        move_score = max(abs(pct_1h) / 0.006, abs(pct_4h) / 0.015)
        if move_score < 1.0 and bias == "neutral":
            continue
        direction = "long" if pct_1h > 0.0 or pct_4h > 0.0 or bias == "uptrend" else "short"
        if bias == "downtrend":
            direction = "short"
        elif bias == "uptrend":
            direction = "long"
        votes.append(
            {
                "symbol": benchmark,
                "direction": direction,
                "bias": bias,
                "pct_1h": pct_1h,
                "pct_4h": pct_4h,
                "move_score": round(move_score, 3),
            }
        )

    aligned = sum(1 for vote in votes if vote["direction"] == signal.direction)
    opposed = sum(1 for vote in votes if vote["direction"] != signal.direction)
    strong_opposed = [
        vote
        for vote in votes
        if vote["direction"] != signal.direction and float(vote["move_score"]) >= 1.0
    ]
    details = {
        "benchmark_context_available": bool(context),
        "votes": votes,
        "aligned": aligned,
        "opposed": opposed,
        "strong_opposed": strong_opposed,
        "macro_risk_mode": getattr(prepared, "macro_risk_mode", None),
        "benchmark_context_stale": benchmark_context_stale,
        "context_snapshot_age_seconds": context_age,
    }
    if len(strong_opposed) >= 2 and opposed > aligned:
        return False, "benchmark_context_conflict", details

    macro_risk_mode = str(getattr(prepared, "macro_risk_mode", "") or "").lower()
    if signal.direction == "long" and macro_risk_mode in {"risk_off", "panic", "stress"}:
        return False, "macro_risk_off_long", details
    return True, None, details


def _regime_long_gate(
    signal: Signal,
    prepared: PreparedSymbol,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Block trend/breakout longs in confirmed bear context unless reversal setup."""
    direction = str(signal.direction or "").lower()
    if direction != "long":
        return True, None, {"regime_gate": "not_long"}

    profile = str(signal.confirmation_profile or "trend_follow")
    family = str(signal.strategy_family or "continuation")
    reversal_profiles = {"countertrend_exhaustion", "divergence_reversal"}
    reversal_setups = {
        "funding_reversal",
        "ls_ratio_extreme",
        "liquidation_heatmap",
        "turtle_soup",
        "wick_trap_reversal",
        "wyckoff_spring",
        "stop_hunt_detection",
        "liquidity_sweep",
        "volume_climax_reversal",
        "absorption",
        "rsi_divergence_bottom",
        "indicator_divergence",
        "cvd_divergence",
        "oi_divergence",
    }
    regime = str(getattr(prepared, "market_regime", "") or "").lower()
    btc_bias = str(getattr(prepared, "btc_bias", None) or signal.btc_bias or "neutral").lower()
    bias_4h = str(getattr(prepared, "bias_4h", "") or "neutral").lower()
    btc_phase = str(getattr(prepared, "btc_phase", "") or "").lower()
    bear_regime = regime in {"bear", "decline", "risk_off"}
    if profile in reversal_profiles or signal.setup_id in reversal_setups or family == "reversal":
        if bear_regime and btc_phase in {"decline", "distribution"}:
            return (
                False,
                "regime_bear_reversal_long_blocked",
                {
                    "regime_gate": "reversal_bear_blocked",
                    "market_regime": regime,
                    "btc_bias": btc_bias,
                    "btc_phase": btc_phase,
                },
            )
        return True, None, {"regime_gate": "reversal_exempt"}
    bear_bias = btc_bias in {"downtrend", "bear"} or bias_4h == "downtrend"
    trend_family = family in {"continuation", "breakout", "trend_follow"} or profile in {
        "trend_follow",
        "breakout_acceptance",
    }
    details = {
        "regime_gate": "checked",
        "market_regime": regime,
        "btc_bias": btc_bias,
        "bias_4h": bias_4h,
        "strategy_family": family,
        "confirmation_profile": profile,
    }
    if bear_regime and bear_bias and trend_family:
        return False, "regime_bear_long_blocked", details
    if bear_bias and trend_family:
        return False, "btc_downtrend_long_blocked", details
    return True, None, details


def _regime_short_gate(
    signal: Signal,
    prepared: PreparedSymbol,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Block trend/orderbook shorts in confirmed bull context unless reversal setup."""
    direction = str(signal.direction or "").lower()
    if direction != "short":
        return True, None, {"regime_gate": "not_short"}

    profile = str(signal.confirmation_profile or "trend_follow")
    family = str(signal.strategy_family or "continuation")
    reversal_profiles = {"countertrend_exhaustion", "divergence_reversal"}
    reversal_setups = {
        "funding_reversal",
        "ls_ratio_extreme",
        "liquidation_heatmap",
        "turtle_soup",
        "wick_trap_reversal",
        "wyckoff_spring",
        "stop_hunt_detection",
        "liquidity_sweep",
        "volume_climax_reversal",
        "absorption",
        "rsi_divergence_bottom",
        "indicator_divergence",
        "cvd_divergence",
        "oi_divergence",
    }
    if profile in reversal_profiles or signal.setup_id in reversal_setups or family == "reversal":
        return True, None, {"regime_gate": "reversal_exempt"}

    regime = str(getattr(prepared, "market_regime", "") or "").lower()
    btc_bias = str(getattr(prepared, "btc_bias", None) or signal.btc_bias or "neutral").lower()
    bias_4h = str(getattr(prepared, "bias_4h", "") or "neutral").lower()
    bull_regime = regime in {"bull", "markup", "risk_on"}
    bull_bias = btc_bias in {"uptrend", "bull"} or bias_4h == "uptrend"
    trend_family = family in {
        "continuation",
        "breakout",
        "trend_follow",
        "orderbook",
        "multi_asset",
    } or profile in {
        "trend_follow",
        "breakout_acceptance",
    }
    details = {
        "regime_gate": "checked",
        "market_regime": regime,
        "btc_bias": btc_bias,
        "bias_4h": bias_4h,
        "strategy_family": family,
        "confirmation_profile": profile,
    }
    if bull_regime and bull_bias and trend_family:
        return False, "regime_bull_short_blocked", details
    if bull_bias and trend_family:
        return False, "btc_uptrend_short_blocked", details
    return True, None, details


def _latest_frame_float(frame: pl.DataFrame | None, column: str) -> float | None:
    if frame is None or frame.is_empty() or column not in frame.columns:
        return None
    try:
        value = frame.item(-1, column)
    except IndexError, TypeError, ValueError:
        return None
    try:
        numeric = float(value)
    except TypeError, ValueError:
        return None
    return numeric if math.isfinite(numeric) else None


def _microstructure_context_for_signal(
    signal: Signal,
    prepared: PreparedSymbol,
) -> MicrostructureContext:
    """Build a direction-aware microstructure snapshot from the prepared symbol."""
    row = {
        "symbol": signal.symbol,
        "direction": signal.direction,
        "price_change_pct": prepared.universe.price_change_pct,
        "funding_rate": prepared.funding_rate,
        "oi_change_pct": prepared.oi_change_pct,
        "global_account_ls_ratio": prepared.global_account_ls_ratio or prepared.global_ls_ratio,
        "top_account_ls_ratio": prepared.top_account_ls_ratio or prepared.ls_ratio,
        "top_position_ls_ratio": prepared.top_position_ls_ratio,
        "taker_ratio": prepared.taker_ratio,
        "taker_buy_base": _latest_frame_float(prepared.work_15m, "taker_buy_base_volume"),
        "volume": _latest_frame_float(prepared.work_15m, "volume"),
        "bid_price": prepared.bid_price,
        "ask_price": prepared.ask_price,
        "depth_imbalance": prepared.depth_imbalance,
        "microprice_bias": prepared.microprice_bias,
        "basis_pct": prepared.basis_pct,
        "liquidation_score": prepared.liquidation_score,
    }
    return build_microstructure_context(row)


def _expand_signal_to_min_stop(
    signal: Signal,
    *,
    min_stop_distance_pct: float,
    min_rr: float,
) -> tuple[Signal, bool]:
    """Widen micro-stops to the runtime minimum and preserve TP1 RR.

    Detectors often anchor stops to tight local structure. That is useful for
    pattern recognition, but the live signal contract has a global minimum stop
    distance to avoid immediate noise stops. Normalize the signal here instead
    of rejecting a valid setup after it has already passed strategy logic.
    """
    entry = float(signal.entry_mid)
    if entry <= 0.0 or min_stop_distance_pct <= 0.0:
        return signal, False
    if signal.stop_distance_pct >= min_stop_distance_pct:
        return signal, False

    min_risk = entry * (min_stop_distance_pct / 100.0)
    rr_floor = max(1.0, float(min_rr))
    tp2_rr = max(rr_floor * 1.5, rr_floor + 0.5)
    reasons = (
        *signal.reasons,
        f"min_stop_normalized={min_stop_distance_pct:.2f}% rr_floor={rr_floor:.2f}",
    )

    if signal.direction == "long":
        stop = entry - min_risk
        tp1 = max(float(signal.take_profit_1), entry + min_risk * rr_floor)
        tp2 = max(float(signal.take_profit_2), tp1, entry + min_risk * tp2_rr)
        tp3 = max(float(signal.tp3), tp2, entry + min_risk * DEFAULT_TARGET_RR[2])
    else:
        stop = entry + min_risk
        tp1 = min(float(signal.take_profit_1), entry - min_risk * rr_floor)
        tp2 = min(float(signal.take_profit_2), tp1, entry - min_risk * tp2_rr)
        tp3 = min(float(signal.tp3), tp2, entry - min_risk * DEFAULT_TARGET_RR[2])

    risk_reward = abs(tp1 - entry) / min_risk if min_risk > 0.0 else signal.risk_reward
    plan = TradePlanBuilder.build(
        direction=signal.direction,
        setup_id=signal.setup_id,
        strategy_family=signal.strategy_family,
        timeframe=signal.timeframe,
        price_anchor=entry,
        atr=max(min_risk, entry * 0.0005),
        stop_loss=stop,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        created_at=signal.created_at,
        ttl_bars=signal.ttl_bars,
        scale_weights=signal.scale_weights,
    )
    if plan is not None:
        stop = plan.stop_loss
        tp1 = plan.tp1
        tp2 = plan.tp2
        tp3 = plan.tp3
        risk_reward = abs(tp1 - entry) / min_risk if min_risk > 0.0 else signal.risk_reward
    if signal.direction == "long":
        ordered = stop < entry < tp1 <= tp2 <= tp3
    else:
        ordered = stop > entry > tp1 >= tp2 >= tp3
    if not ordered:
        LOGGER.exception(
            (
                "stop expansion produced invalid price ordering | symbol=%s setup=%s "
                "direction=%s entry=%.8f stop=%.8f tp1=%.8f tp2=%.8f tp3=%.8f"
            ),
            signal.symbol,
            signal.setup_id,
            signal.direction,
            entry,
            stop,
            tp1,
            tp2,
            tp3,
        )
        return signal, False
    return (
        replace(
            signal,
            stop=stop,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            risk_reward=risk_reward,
            target_integrity_status=(
                plan.integrity_status if plan is not None else signal.target_integrity_status
            ),
            entry_plan_status=plan.integrity_status
            if plan is not None
            else signal.entry_plan_status,
            reasons=reasons,
        ),
        True,
    )


def _primary_freshness_window(
    prepared: PreparedSymbol,
    settings: BotSettings,
) -> tuple[str, timedelta]:
    timeframe = str(getattr(prepared, "primary_timeframe", "15m") or "15m")
    if timeframe == "1h":
        return timeframe, timedelta(hours=settings.filters.freshness_1h_hours)
    if timeframe == "4h":
        return timeframe, timedelta(hours=settings.filters.freshness_4h_hours)
    if timeframe == "5m":
        return timeframe, timedelta(minutes=settings.filters.freshness_5m_minutes)
    return "15m", timedelta(minutes=settings.filters.freshness_15m_minutes)


def _frame_for_timeframe(
    prepared: PreparedSymbol,
    timeframe: str,
) -> pl.DataFrame | None:
    if timeframe == "5m":
        return prepared.work_5m
    if timeframe == "1h":
        return prepared.work_1h
    if timeframe == "4h":
        return prepared.work_4h
    return prepared.work_15m


_TIMEFRAME_INTERVAL_SECONDS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}


def _timeframe_interval_seconds(timeframe: str) -> int:
    return _TIMEFRAME_INTERVAL_SECONDS.get(timeframe, 900)


def _expected_min_close(now: datetime, timeframe: str) -> datetime:
    """Return the open time of the current candle period (latest expected close)."""
    interval_secs = _timeframe_interval_seconds(timeframe)
    epoch = int(now.timestamp())
    period_start = epoch - (epoch % interval_secs)
    return datetime.fromtimestamp(period_start, tz=UTC)


def _frame_is_fresh(
    frame: pl.DataFrame,
    max_age: timedelta,
    *,
    timeframe: str | None = None,
) -> bool:
    if frame.is_empty() or "close_time" not in frame.columns:
        return False
    try:
        last_close = frame["close_time"].item(-1)
        if isinstance(last_close, str):
            last_close = datetime.fromisoformat(last_close)
        elif isinstance(last_close, datetime):
            pass
        elif isinstance(last_close, (int, float)):
            last_close = datetime.fromtimestamp(float(last_close), tz=UTC)
        else:
            LOGGER.debug(
                "Freshness degraded: unsupported close_time type %s",
                type(last_close).__name__,
            )
            return False

        if last_close.tzinfo is None:
            last_close = last_close.replace(tzinfo=UTC)
        else:
            last_close = last_close.astimezone(UTC)
    except DEFENSIVE_EXC as exc:
        LOGGER.debug("Freshness degraded: failed to normalize close_time (%s)", exc)
        return False

    try:
        now = datetime.now(UTC)
        delta = now - last_close
    except DEFENSIVE_EXC as exc:
        LOGGER.debug("Freshness degraded: failed to compute freshness delta (%s)", exc)
        return False

    # Forming candle: close_time is still in the future - stream is live.
    if delta.total_seconds() < 0:
        return True

    if timeframe:
        # Compare against the latest closed-bar boundary instead of raw age.
        # After dropping an in-progress 15m tail, last_close can legitimately
        # be one full interval old at the open of the next bar.
        threshold = _expected_min_close(now, timeframe) - max_age
        return last_close >= threshold

    return delta <= max_age


def _entry_staleness_gate(
    signal: Signal,
    prepared: PreparedSymbol,
    settings: BotSettings,
    *,
    atr_pct: float,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Reject when mark/ticker has already moved too far from the planned entry."""
    current_price = prepared.mark_price or prepared.ticker_price
    entry_price = float(signal.entry_mid_raw)
    if (
        current_price is None
        or not math.isfinite(float(current_price))
        or float(current_price) <= 0.0
        or entry_price <= 0.0
        or atr_pct <= 0.0
    ):
        return True, None, None

    # fix-sl-A: reject entry if price has already moved > N*ATR from signal entry
    deviation = abs(float(current_price) - entry_price) / entry_price
    atr_mult = float(getattr(settings.filters, "max_entry_deviation_atr_mult", 1.2))
    threshold = max(0.001, atr_mult * atr_pct / 100.0)
    details: dict[str, Any] = {
        "entry_price": entry_price,
        "current_price": float(current_price),
        "entry_deviation_pct": round(deviation * 100.0, 4),
        "atr_pct": atr_pct,
        "max_deviation_pct": round(threshold * 100.0, 4),
        "atr_mult": atr_mult,
    }
    if deviation > threshold:
        return (
            False,
            "entry_staleness",
            details,
        )
    return True, None, details


def _market_atr_floor(prepared: PreparedSymbol, settings: BotSettings) -> float:
    """Return an ATR floor adapted to current volatility conditions.

    Static ATR floors can reject usable setups in confirmed low-volatility
    markets. When 1h ADX is low, and especially when Bollinger width is also
    narrow, reduce the configured floor instead of turning a market regime
    mismatch into a hard no-signal state. Existing configurations below 0.20
    are preserved so this helper never tightens an already-lower threshold.
    """
    atr = _latest_frame_float(prepared.work_15m, "atr_pct")
    if atr is None:
        atr = _latest_frame_float(prepared.work_1h, "atr_pct")
    if atr is not None and atr <= 0.0:
        return 0.0  # ATR pct: non-positive volatility data makes the floor invalid.

    base_min = float(settings.filters.min_atr_pct)
    if base_min <= 0.20:
        return base_min

    adx_val = 0.0
    if not prepared.work_1h.is_empty() and "adx14" in prepared.work_1h.columns:
        try:
            adx_val = float(prepared.work_1h.item(-1, "adx14") or 0.0)
        except DEFENSIVE_EXC as exc:
            LOGGER.debug("ATR floor ADX read failed for %s: %s", prepared.symbol, exc)
            adx_val = 0.0

    bb_width: float | None = None
    atr_frame = prepared.work_15m
    if not atr_frame.is_empty() and "bb_width" in atr_frame.columns:
        try:
            bb_width = float(atr_frame.item(-1, "bb_width") or 0.0)
        except DEFENSIVE_EXC as exc:
            LOGGER.debug("ATR floor Bollinger width read failed for %s: %s", prepared.symbol, exc)
            bb_width = None

    low_adx = 0.0 < adx_val < 18.0
    narrow_bb = bb_width is not None and bb_width < 3.0
    if low_adx and narrow_bb:
        return max(0.20, base_min * 0.55)
    if low_adx:
        return max(0.20, base_min * 0.70)
    return base_min


def _record_atr_sample(setup_id: str, atr_pct: float, *, passed: bool) -> None:
    try:
        diagnostics = get_global_diagnostics()
        if diagnostics is not None:
            diagnostics.record_atr_sample(setup_id, atr_pct, passed=passed)
    except DEFENSIVE_EXC:
        LOGGER.debug("ATR diagnostic sample recording failed")


def apply_global_filters(
    signal: Signal,
    prepared: PreparedSymbol,
    settings: BotSettings,
    confluence_engine: ConfluenceEngine,
) -> tuple[bool, Signal, str | None, ScoringResult | None, dict[str, Any] | None] | None:
    try:
        return _run_filter_pipeline(signal, prepared, settings, confluence_engine)
    except DEFENSIVE_EXC as exc:
        LOGGER.exception(
            "filter_pipeline_crash",
            extra={"exc": str(exc), "setup_id": getattr(signal, "setup_id", "unknown")},
        )
        return None


def _run_filter_pipeline(
    signal: Signal,
    prepared: PreparedSymbol,
    settings: BotSettings,
    confluence_engine: ConfluenceEngine,
) -> tuple[bool, Signal, str | None, ScoringResult | None, dict[str, Any] | None]:
    """Apply hard gates, scoring, and optional ML enhancement.

    Pipeline order (strict):
      1. Data freshness gates (15m, 1h)
      1b. Entry staleness (mark vs entry_mid vs ATR)
      2. Mark price deviation guard
      3. Spread gate
      4. ATR gate
      5. Stop distance gate
      6. Risk/Reward gate
      7. Scoring engine
      8. ML enhancement (if enabled and confident)
      9. Minimum score gate
    """
    passed = list(signal.passed_filters)
    deep_analysis_asset = is_deep_analysis_symbol(prepared, settings)

    configured_primary = configured_primary_timeframe(settings, signal.symbol)
    actual_primary = str(getattr(prepared, "primary_timeframe", "15m") or "15m")
    if actual_primary != configured_primary:
        passed.append(f"primary_timeframe_fallback:{configured_primary}")

    base = replace(
        signal,
        quote_volume=prepared.universe.quote_volume,
        oi_change_pct=prepared.oi_change_pct,
        funding_rate=prepared.funding_rate,
        spread_bps=prepared.spread_bps,
    )

    def _reject(
        reason: str,
        updated_signal: Signal,
        scoring: ScoringResult | None = None,
        details: dict[str, Any] | None = None,
    ) -> tuple[bool, Signal, str | None, ScoringResult | None, dict[str, Any] | None]:
        return (
            False,
            replace(updated_signal, passed_filters=tuple(passed)),
            reason,
            scoring,
            details,
        )

    regime_ok, regime_reason, regime_details = _regime_long_gate(base, prepared)
    if getattr(settings.filters, "regime_filter_enabled", True):
        if not regime_ok:
            LOGGER.info(
                "%s/%s: regime long gate reject | reason=%s details=%s",
                signal.symbol,
                signal.setup_id,
                regime_reason,
                regime_details,
            )
            return _reject(regime_reason or "regime_long_blocked", base, details=regime_details)

        short_regime_ok, short_regime_reason, short_regime_details = _regime_short_gate(
            base, prepared
        )
        if not short_regime_ok:
            LOGGER.info(
                "%s/%s: regime short gate reject | reason=%s details=%s",
                signal.symbol,
                signal.setup_id,
                short_regime_reason,
                short_regime_details,
            )
            return _reject(
                short_regime_reason or "regime_short_blocked",
                base,
                details=short_regime_details,
            )

    profile = str(getattr(base, "confirmation_profile", "trend_follow"))
    mtf_ok, mtf_reason, mtf_details = evaluate_mtf_gate(
        prepared,
        base.direction,
        confirmation_profile=profile,
        strict_data_quality=bool(getattr(settings.runtime, "strict_data_quality", True)),
    )
    passed.append("mtf_precheck")
    if mtf_ok:
        passed.append("mtf_gate")
    else:
        LOGGER.info(
            "%s/%s: mtf precheck failed (orchestrator confluence gate) | reason=%s details=%s",
            signal.symbol,
            signal.setup_id,
            normalize_mtf_reject_reason(mtf_reason),
            mtf_details,
        )

    # --- 1. Data freshness ---
    if filter_stage_enabled(settings, "freshness"):
        primary_timeframe, primary_freshness = _primary_freshness_window(prepared, settings)
        if deep_analysis_asset:
            passed.append("deep_analysis_policy")
        primary_frame = _frame_for_timeframe(prepared, primary_timeframe)
        if primary_frame is None or not _frame_is_fresh(
            primary_frame,
            primary_freshness,
            timeframe=primary_timeframe,
        ):
            LOGGER.info(
                "%s/%s: freshness fail | timeframe=%s freshness_limit=%s",
                signal.symbol,
                signal.setup_id,
                primary_timeframe,
                str(primary_freshness),
            )
            return _reject(f"stale_{primary_timeframe}", base)
        passed.append(
            "fresh_15m" if primary_timeframe == "15m" else f"fresh_primary_{primary_timeframe}"
        )
        if not _frame_is_fresh(
            prepared.work_1h,
            timedelta(hours=settings.filters.freshness_1h_hours),
            timeframe="1h",
        ):
            LOGGER.info(
                "%s/%s: freshness fail | timeframe=1h freshness_limit=%s",
                signal.symbol,
                signal.setup_id,
                str(timedelta(hours=settings.filters.freshness_1h_hours)),
            )
            return _reject("stale_1h", base)
        passed.append("fresh_1h")
        if prepared.work_4h is None or not _frame_is_fresh(
            prepared.work_4h,
            timedelta(hours=settings.filters.freshness_4h_hours),
            timeframe="4h",
        ):
            LOGGER.info(
                "%s/%s: freshness fail | timeframe=4h freshness_limit=%s",
                signal.symbol,
                signal.setup_id,
                str(timedelta(hours=settings.filters.freshness_4h_hours)),
            )
            return _reject("stale_4h", base)
        passed.append("fresh_4h")
    else:
        primary_timeframe, _ = _primary_freshness_window(prepared, settings)
        primary_frame = _frame_for_timeframe(prepared, primary_timeframe)
        passed.append("freshness_stage_disabled")

    # --- 1b. Entry staleness (fix-sl-A) ---
    pre_atr_frame = primary_frame if primary_frame is not None else prepared.work_15m
    pre_atr_pct = 0.0
    if not pre_atr_frame.is_empty() and "atr_pct" in pre_atr_frame.columns:
        raw_atr = pre_atr_frame.item(-1, "atr_pct")
        if raw_atr is not None and not (isinstance(raw_atr, float) and math.isnan(raw_atr)):
            pre_atr_pct = float(raw_atr)
    if filter_stage_enabled(settings, "entry_staleness"):
        staleness_ok, staleness_reason, staleness_details = _entry_staleness_gate(
            base,
            prepared,
            settings,
            atr_pct=pre_atr_pct,
        )
        if not staleness_ok:
            LOGGER.info(
                "%s/%s: entry_staleness reject | details=%s",
                signal.symbol,
                signal.setup_id,
                staleness_details,
            )
            return _reject(staleness_reason or "entry_staleness", base, details=staleness_details)
        passed.append("entry_staleness_ok")
    else:
        passed.append("entry_staleness_stage_disabled")

    # --- 2. Mark price sanity ---
    if filter_stage_enabled(settings, "mark_deviation") and (
        prepared.mark_price is not None
        and prepared.mark_price > 0
        and prepared.ticker_price is not None
        and prepared.ticker_price > 0
    ):
        deviation = abs(prepared.mark_price - prepared.ticker_price) / prepared.ticker_price
        mark_price_details = {
            "mark_price": prepared.mark_price,
            "comparison_price": prepared.ticker_price,
            "comparison_source": "ws_ticker",
            "comparison_age_seconds": prepared.ticker_price_age_seconds,
            "mark_price_age_seconds": prepared.mark_price_age_seconds,
            "deviation_pct": deviation,
        }
        if deviation > settings.filters.max_mark_price_deviation_pct:
            return _reject("mark_price_deviation", base, details=mark_price_details)
    passed.append("mark_price_ok")

    # --- 3. Spread ---
    if filter_stage_enabled(settings, "spread"):
        if prepared.spread_bps is None:
            return _reject("spread_unavailable", base)
        if prepared.spread_bps > settings.filters.max_spread_bps:
            return _reject("spread_too_wide", base)
        passed.append("spread_ok")
    else:
        passed.append("spread_stage_disabled")

    # --- 4. ATR ---
    if filter_stage_enabled(settings, "atr"):
        atr_frame = primary_frame if primary_frame is not None else prepared.work_15m
        if atr_frame.is_empty() or "atr_pct" not in atr_frame.columns:
            return _reject("atr_unavailable", replace(base, atr_pct=0.0))
        atr_pct_raw = atr_frame.item(-1, "atr_pct")
        if atr_pct_raw is None or (isinstance(atr_pct_raw, float) and math.isnan(atr_pct_raw)):
            return _reject("atr_nan", replace(base, atr_pct=0.0))
        atr_pct = float(atr_pct_raw)
        effective_min_atr = _market_atr_floor(prepared, settings)
        atr_gate_passed = atr_pct >= effective_min_atr
        _record_atr_sample(signal.setup_id, atr_pct, passed=atr_gate_passed)
        if effective_min_atr < float(settings.filters.min_atr_pct):
            passed.append(f"atr_floor_relaxed_to_{effective_min_atr:.3f}")
        if not atr_gate_passed:
            LOGGER.info(
                "%s/%s: atr_too_low | atr_pct=%.4f effective_min_atr=%.4f min_atr_pct=%.4f "
                "(config: filters.min_atr_pct=%.4f)",
                signal.symbol,
                signal.setup_id,
                atr_pct,
                effective_min_atr,
                settings.filters.min_atr_pct,
                settings.filters.min_atr_pct,
            )
            return _reject(
                "atr_too_low",
                replace(base, atr_pct=atr_pct),
                details={
                    "atr_pct": atr_pct,
                    "effective_min_atr": effective_min_atr,
                    "config_min_atr": settings.filters.min_atr_pct,
                },
            )
        if atr_pct > settings.filters.max_atr_pct:
            return _reject("atr_too_high", replace(base, atr_pct=atr_pct))
        passed.append("atr_ok")
    else:
        passed.append("atr_stage_disabled")
        atr_frame = primary_frame if primary_frame is not None else prepared.work_15m
        atr_pct = float(atr_frame.item(-1, "atr_pct") or 0.0) if not atr_frame.is_empty() else 0.0

    # --- 4b. ADX policy (setup/family aware) ---
    adx_1h = 0.0
    if not prepared.work_1h.is_empty():
        adx_1h = float(prepared.work_1h.item(-1, "adx14") or 0.0)
    setup_overrides = settings.filters.setups.get(signal.setup_id, {})
    min_adx_1h = float(setup_overrides.get("min_adx_1h", settings.filters.min_adx_1h))
    adx_penalty_factor = float(setup_overrides.get("adx_penalty_factor", 0.85))
    adx_policy = _resolve_adx_policy(signal)
    market_regime = str(getattr(prepared, "market_regime", "neutral") or "neutral").lower()
    if market_regime in {"neutral", "ranging", "choppy"} and adx_policy == _ADX_POLICY_HARD_GATE:
        adx_policy = _ADX_POLICY_PENALTY
        adx_penalty_factor = max(adx_penalty_factor, 0.88)
        LOGGER.info(
            "%s/%s: ADX hard gate -> penalty (ranging market) | adx_1h=%.1f min=%.1f",
            signal.symbol,
            signal.setup_id,
            adx_1h,
            min_adx_1h,
        )
        passed.append("adx_ranging_market_downgrade")
    if deep_analysis_asset:
        min_adx_1h = min(min_adx_1h, 14.0 if primary_timeframe == "15m" else 12.0)
        if adx_policy == _ADX_POLICY_HARD_GATE:
            adx_policy = _ADX_POLICY_PENALTY
            adx_penalty_factor = max(adx_penalty_factor, 0.90)
            LOGGER.info(
                (
                    "deep-analysis ADX hard gate downgraded to score penalty | "
                    "symbol=%s setup=%s primary_timeframe=%s min_adx_1h=%.2f"
                ),
                signal.symbol,
                signal.setup_id,
                primary_timeframe,
                min_adx_1h,
            )
    adx_penalty_applied = False
    if adx_1h > 0.0 and adx_1h < min_adx_1h:
        if adx_policy == _ADX_POLICY_HARD_GATE:
            details = {
                "adx_policy": adx_policy,
                "adx_1h": adx_1h,
                "min_adx_1h": min_adx_1h,
                "setup_id": signal.setup_id,
                "strategy_family": signal.strategy_family,
                "primary_timeframe": primary_timeframe,
            }
            return _reject("regime_not_suitable", replace(base, atr_pct=atr_pct), details=details)
        adx_penalty_applied = True
        passed.append("adx_1h_penalized")
    else:
        passed.append("adx_1h_ok")

    # 4h ranging no longer hard-blocks breakout strategies - a ranging 4h already
    # lowers the MTF alignment score (0.5 instead of 1.0), which reduces confidence
    # appropriately. Hard-blocking caused too many missed setups on symbols where 4h
    # is transitional but 1h clearly shows direction.
    passed.append("regime_ok")

    dominant_1h = str(
        getattr(prepared, "regime_1h_confirmed", None)
        or getattr(prepared, "bias_1h", None)
        or "neutral"
    ).lower()
    if (signal.direction == "long" and dominant_1h == "downtrend") or (
        signal.direction == "short" and dominant_1h == "uptrend"
    ):
        trend_conflict = True
    else:
        trend_conflict = False

    trend_conflict_penalty_applied = False
    trend_conflict_penalty_factor = float(
        setup_overrides.get("trend_conflict_penalty_factor", 0.88)
    )
    btc_phase = str(getattr(prepared, "btc_phase", "") or "").lower()
    btc_decline_penalty_applied = False
    btc_decline_penalty_factor = float(setup_overrides.get("btc_decline_penalty_factor", 0.90))
    if trend_conflict:
        trend_details = {
            "signal_direction": signal.direction,
            "dominant_1h": dominant_1h,
            "setup_id": signal.setup_id,
            "strategy_family": signal.strategy_family,
            "confirmation_profile": signal.confirmation_profile,
        }
        if not _uses_soft_trend_conflict(signal):
            return _reject("trend_conflict_1h", base, details=trend_details)
        trend_conflict_penalty_applied = True
        passed.append("trend_conflict_1h_penalized")
    else:
        passed.append("trend_context_ok")

    if (
        _uses_soft_trend_conflict(signal)
        and signal.direction == "long"
        and btc_phase in {"decline", "distribution"}
    ):
        btc_decline_penalty_applied = True
        passed.append("btc_decline_penalty_eligible")
    elif btc_phase:
        passed.append("btc_phase_ok")

    benchmark_ok, benchmark_reason, benchmark_details = _benchmark_context_guard(signal, prepared)
    if not benchmark_ok and not deep_analysis_asset:
        return _reject(
            benchmark_reason or "benchmark_context_conflict", base, details=benchmark_details
        )
    if benchmark_ok:
        passed.append("benchmark_context_ok")
    else:
        passed.append("benchmark_context_deep_override")
    if benchmark_details.get("benchmark_context_stale"):
        passed.append("benchmark_context_stale")

    # Compute delta_ratio from 15m candles (CVD proxy)
    delta_ratio: float | None = None
    if not prepared.work_15m.is_empty() and "delta_ratio" in prepared.work_15m.columns:
        raw_delta = prepared.work_15m.item(-1, "delta_ratio")
        if raw_delta is not None:
            delta_ratio = float(raw_delta)
    micro_context = _microstructure_context_for_signal(signal, prepared)
    if micro_context.confidence >= 0.35:
        passed.append(f"microstructure_{micro_context.label}")
    else:
        passed.append("microstructure_sparse")
    if micro_context.warnings:
        passed.append("microstructure_warning")

    updated = replace(
        signal,
        spread_bps=prepared.spread_bps,
        atr_pct=atr_pct,
        quote_volume=prepared.universe.quote_volume,
        oi_change_pct=prepared.oi_change_pct,
        funding_rate=prepared.funding_rate,
        orderflow_delta_ratio=delta_ratio,
        mark_price=prepared.mark_price,
        volume_ratio=_latest_frame_float(prepared.work_15m, "volume_ratio20"),
        adx_1h=adx_1h,
        premium_zscore_5m=prepared.premium_zscore_5m,
        premium_slope_5m=prepared.premium_slope_5m,
        ls_ratio=prepared.ls_ratio or prepared.global_ls_ratio,
        microstructure_bias_score=micro_context.bias_score,
        microstructure_confidence=micro_context.confidence,
        microstructure_label=micro_context.label,
        microstructure_reason=micro_context.reason_line(),
        microstructure_warnings=micro_context.warnings,
        btc_bias=prepared.btc_bias,
        eth_bias=prepared.eth_bias,
        sol_bias=prepared.sol_bias,
        xau_bias=prepared.xau_bias,
        xag_bias=prepared.xag_bias,
        pax_bias=prepared.pax_bias,
        passed_filters=tuple(passed),
    )

    # --- 5. Stop distance ---
    if filter_stage_enabled(settings, "stop"):
        min_stop_distance_pct = float(settings.tracking.min_stop_distance_pct)
        effective_min_rr = float(setup_overrides.get("min_rr", settings.filters.min_risk_reward))
        updated, stop_expanded = _expand_signal_to_min_stop(
            updated,
            min_stop_distance_pct=min_stop_distance_pct,
            min_rr=effective_min_rr,
        )
        if stop_expanded:
            passed.append("stop_expanded_to_min")
        stop_epsilon = 1e-6
        if updated.stop_distance_pct + stop_epsilon < min_stop_distance_pct:
            return _reject(
                "stop_too_tight",
                updated,
                details={
                    "stop_distance_pct": updated.stop_distance_pct,
                    "min_stop_distance_pct": min_stop_distance_pct,
                    "global_min_stop_distance_pct": settings.tracking.min_stop_distance_pct,
                    "deep_analysis_policy": deep_analysis_asset,
                    "primary_timeframe": primary_timeframe,
                },
            )
        if updated.stop_distance_pct > settings.tracking.max_stop_distance_pct:
            return _reject("stop_too_wide", updated)
        updated = replace(updated, passed_filters=(*updated.passed_filters, "stop_ok"))
    else:
        passed.append("stop_stage_disabled")

    # --- 6. Risk / Reward (runtime gate uses TP1; TP2 RR remains analytical) ---
    if filter_stage_enabled(settings, "rr"):
        risk = abs(updated.entry_mid - updated.stop)
        reward_tp1 = abs(updated.take_profit_1 - updated.entry_mid)
        rr_tp1 = (reward_tp1 / risk) if risk > 0 else 0.0
        rr_epsilon = 1e-9
        effective_min_rr = float(setup_overrides.get("min_rr", settings.filters.min_risk_reward))
        if rr_tp1 + rr_epsilon < effective_min_rr:
            return _reject(
                "risk_reward_too_low",
                updated,
                details={
                    "gate_rr_target": "tp1",
                    "rr_tp1": rr_tp1,
                    "rr_tp2": updated.risk_reward,
                    "min_rr_required": effective_min_rr,
                    "global_min_rr": settings.filters.min_risk_reward,
                    "setup_id": signal.setup_id,
                    "deep_analysis_policy": deep_analysis_asset,
                    "primary_timeframe": primary_timeframe,
                },
            )
        updated = replace(updated, passed_filters=(*updated.passed_filters, "rr_ok"))
    else:
        passed.append("rr_stage_disabled")

    # --- 7. Scoring (ConfluenceEngine - unified path) ---
    scoring_result: ScoringResult | None = None
    if filter_stage_enabled(settings, "scoring") and settings.scoring.enabled:
        confluence_result = confluence_engine.score(updated, prepared)
        updated = replace(updated, score=confluence_result.final_score)
        scoring_result = confluence_result.to_scoring_result()
        passed = list(updated.passed_filters)
        passed.append("scoring_applied")
        updated = replace(updated, passed_filters=tuple(passed))
    elif not filter_stage_enabled(settings, "scoring"):
        passed.append("scoring_stage_disabled")

    if adx_penalty_applied:
        # Apply the ADX penalty before the min-score gate so weak-trend
        # signals are rejected using their final effective score.
        pre_penalty_score = updated.score
        adjusted_score = pre_penalty_score * adx_penalty_factor
        updated = replace(updated, score=adjusted_score)
        penalty_delta = round(adjusted_score - pre_penalty_score, 6)
        if scoring_result is not None:
            scoring_result = replace(
                scoring_result,
                final_score=adjusted_score,
                adjustments={
                    **scoring_result.adjustments,
                    "adx_policy_penalty": penalty_delta,
                },
            )
        else:
            scoring_result = ScoringResult(
                base_score=pre_penalty_score,
                adjustments={"adx_policy_penalty": penalty_delta},
                final_score=adjusted_score,
                setup_id=updated.setup_id,
            )
        updated = replace(
            updated,
            passed_filters=(*updated.passed_filters, "adx_penalty_applied"),
        )

    if trend_conflict_penalty_applied:
        pre_penalty_score = updated.score
        adjusted_score = pre_penalty_score * trend_conflict_penalty_factor
        updated = replace(updated, score=adjusted_score)
        penalty_delta = round(adjusted_score - pre_penalty_score, 6)
        if scoring_result is not None:
            scoring_result = replace(
                scoring_result,
                final_score=adjusted_score,
                adjustments={
                    **scoring_result.adjustments,
                    "trend_conflict_1h_penalty": penalty_delta,
                },
            )
        updated = replace(
            updated,
            passed_filters=(*updated.passed_filters, "trend_conflict_1h_penalty_applied"),
        )

    if btc_decline_penalty_applied:
        pre_penalty_score = updated.score
        adjusted_score = pre_penalty_score * btc_decline_penalty_factor
        updated = replace(updated, score=adjusted_score)
        penalty_delta = round(adjusted_score - pre_penalty_score, 6)
        if scoring_result is not None:
            scoring_result = replace(
                scoring_result,
                final_score=adjusted_score,
                adjustments={
                    **scoring_result.adjustments,
                    "btc_decline_countertrend_penalty": penalty_delta,
                },
            )
        else:
            scoring_result = ScoringResult(
                base_score=pre_penalty_score,
                adjustments={"btc_decline_countertrend_penalty": penalty_delta},
                final_score=adjusted_score,
                setup_id=updated.setup_id,
            )
        updated = replace(
            updated,
            passed_filters=(*updated.passed_filters, "btc_decline_penalty_applied"),
        )

    # --- 9. Minimum score gate (final gate after ALL adjustments) ---
    if filter_stage_enabled(settings, "min_score"):
        effective_min_score = float(settings.filters.min_score)
        if deep_analysis_asset:
            deep_score_floor = 0.48 if primary_timeframe in {"1h", "4h"} else 0.50
            if signal.setup_id in {"bos_choch", "liquidation_heatmap"}:
                deep_score_floor = 0.40
            effective_min_score = min(effective_min_score, deep_score_floor)
        if effective_min_score > 0.0 and updated.score < effective_min_score:
            score_reason = "adx_penalty_score_too_low" if adx_penalty_applied else "score_too_low"
            score_details = {
                "score": updated.score,
                "min_score_required": effective_min_score,
                "global_min_score": settings.filters.min_score,
                "deep_analysis_policy": deep_analysis_asset,
                "primary_timeframe": primary_timeframe,
            }
            if adx_penalty_applied:
                score_details.update(
                    {
                        "adx_policy": adx_policy,
                        "adx_1h": adx_1h,
                        "min_adx_1h": min_adx_1h,
                        "adx_penalty_factor": adx_penalty_factor,
                    }
                )
            return _reject(
                score_reason,
                updated,
                scoring_result,
                details=score_details,
            )
        updated = replace(updated, passed_filters=(*updated.passed_filters, "min_score_ok"))
    else:
        updated = replace(
            updated,
            passed_filters=(*updated.passed_filters, "min_score_stage_disabled"),
        )

    return True, updated, None, scoring_result, None

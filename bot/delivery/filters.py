"""Signal filtering pipeline."""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bot.policy.mtf import evaluate_mtf_gate, normalize_mtf_reject_reason
from engine.contract import resolve_target_rr
from engine.domain.regime_gates import effective_market_regime
from engine.errors import DEFENSIVE_EXC
from engine.features.microstructure import MicrostructureContext, build_microstructure_context
from engine.runtime_policy import configured_primary_timeframe, is_deep_analysis_symbol

from ..diagnostics.signals import get_global_diagnostics
from .filter_stages import filter_stage_enabled
from .scoring import ScoringResult
from .trade_plan import TradePlanBuilder

if TYPE_CHECKING:
    import polars as pl

    from engine.domain.config import BotSettings
    from engine.domain.schemas import PreparedSymbol, Signal

    from .confluence import ConfluenceEngine


LOGGER = logging.getLogger(__name__)

# п.26: per-strategy SL-rate cache. Updated from delivery_orchestrator via
# update_strategy_sl_rates(). Strategies with >70% SL rate get a score penalty.
_STRATEGY_SL_RATES: dict[str, float] = {}
_STRATEGY_SL_SAMPLE_COUNTS: dict[str, int] = {}
_SL_PENALTY_THRESHOLD = 0.70  # sl_rate above this triggers penalty
_SL_PENALTY_MAX_MULT = 0.80  # floor multiplier at 100% sl rate

_SMC_HTF_SETUPS = frozenset(
    {"order_block", "fvg_setup", "liquidity_sweep", "structure_break_retest", "bos_choch"}
)
_MIN_STOP_ATR_MULT = 1.5
_COMPRESSION_ADX_CEILING = 20.0
_COMPRESSION_ATR_RATIO = 0.8
# squeeze_setup excluded: fires only on BB/KC release (see _is_compression_regime docstring).
_COMPRESSION_SHORT_BLOCK_SETUPS = frozenset({"volume_anomaly", "keltner_breakout"})


def _effective_min_stop_distance_pct(
    settings: BotSettings,
    atr_pct: float,
) -> float:
    """answers50 P0: floor stop at max(config%, 1.5×ATR) for volatile symbols."""
    base = float(settings.tracking.min_stop_distance_pct)
    if atr_pct > 0.0:
        return max(base, _MIN_STOP_ATR_MULT * atr_pct)
    return base


def _is_compression_regime(
    prepared: PreparedSymbol,
    *,
    adx_1h: float,
) -> bool:
    """answers50 Q5/Q6/Q41: pre-expansion chop — ADX<20 and ATR still below 50-bar avg.

    squeeze_setup is intentionally excluded from ``_COMPRESSION_SHORT_BLOCK_SETUPS``:
    its detector only fires on BB/KC release (post-compression). When ATR has already
    expanded vs the 50-bar mean we are no longer in pre-expansion compression even if
    ADX is still lagging below 20.
    """
    if adx_1h <= 0.0 or adx_1h >= _COMPRESSION_ADX_CEILING:
        return False
    work = prepared.work_15m
    if work is not None and not work.is_empty() and "atr14" in work.columns:
        atr_series = work["atr14"].drop_nulls()
        if atr_series.len() >= 50:
            current = float(atr_series[-1] or 0.0)
            avg50 = float(atr_series.tail(50).mean() or 0.0)
            if avg50 > 0.0:
                return current / avg50 < _COMPRESSION_ATR_RATIO
    return True


def update_strategy_sl_rates(
    rates: dict[str, float], *, sample_counts: dict[str, int] | None = None
) -> None:
    """Refresh the per-strategy SL-rate cache (п.26). rates: setup_id → sl_rate 0..1."""
    _STRATEGY_SL_RATES.clear()
    _STRATEGY_SL_RATES.update(rates)
    _STRATEGY_SL_SAMPLE_COUNTS.clear()
    if sample_counts:
        _STRATEGY_SL_SAMPLE_COUNTS.update(sample_counts)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _liquidity_tier(prepared: PreparedSymbol, settings: BotSettings) -> str:
    quote_vol = float(getattr(prepared.universe, "quote_volume", 0.0) or 0.0)
    if quote_vol <= 0.0:
        quote_vol = float(getattr(prepared, "quote_volume", 0.0) or 0.0)
    core_min = float(settings.universe.min_quote_volume_usd)
    radar_min = float(settings.universe.radar.min_quote_volume_usd)
    bucket = str(getattr(prepared.universe, "shortlist_bucket", "") or "").lower()
    if bucket == "radar" or (quote_vol >= radar_min and quote_vol < core_min):
        return "radar"
    return "core"


def _tier_max_spread_bps(prepared: PreparedSymbol, settings: BotSettings) -> float:
    if _liquidity_tier(prepared, settings) == "radar":
        return float(settings.universe.radar_max_spread_bps)
    return float(settings.filters.max_spread_bps)


def _signal_age_gate(
    signal: Signal,
    settings: BotSettings,
    *,
    primary_timeframe: str,
) -> tuple[bool, str | None]:
    max_bars = int(getattr(settings.filters, "max_signal_age_bars", 0) or 0)
    if max_bars <= 0:
        return True, None
    tf_seconds = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}.get(primary_timeframe, 900)
    created = signal.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    age_seconds = (datetime.now(UTC) - created.astimezone(UTC)).total_seconds()
    if age_seconds > max_bars * tf_seconds:
        return False, "signal_too_old"
    return True, None


def _htf_zone_confluence_adjustment(
    signal: Signal,
    prepared: PreparedSymbol,
    settings: BotSettings,
) -> tuple[float, str | None, str | None]:
    """Return (score_delta, pass_note, reject_reason)."""
    if signal.setup_id not in _SMC_HTF_SETUPS:
        return 0.0, None, None
    structure_4h = str(getattr(prepared, "structure_4h", "") or "").lower()
    if settings.filters.htf_zone_confluence_required:
        if signal.direction == "long" and structure_4h == "downtrend":
            return 0.0, None, "htf_structure_conflict_4h"
        if signal.direction == "short" and structure_4h == "uptrend":
            return 0.0, None, "htf_structure_conflict_4h"
    if prepared.work_1h.is_empty():
        return 0.0, None, None
    try:
        from ..setups.smc import latest_fvg_zone, latest_order_block

        close = (
            float(prepared.work_15m.item(-1, "close")) if not prepared.work_15m.is_empty() else 0.0
        )
        zone = latest_order_block(
            prepared.work_1h,
            swing_length=3,
            include_unconfirmed_tail=True,
            current_price=close or None,
        )
        if zone is None:
            zone = latest_fvg_zone(prepared.work_1h)
        if zone is None:
            return 0.0, None, None
        entry_low = min(signal.entry_low, signal.entry_high)
        entry_high = max(signal.entry_low, signal.entry_high)
        zone_low = min(zone.bottom, zone.top)
        zone_high = max(zone.bottom, zone.top)
        overlaps = entry_low <= zone_high and entry_high >= zone_low
        if overlaps:
            bonus = float(settings.filters.htf_zone_confluence_bonus)
            return bonus, "htf_zone_overlap_bonus", None
    except DEFENSIVE_EXC:
        LOGGER.debug("htf zone confluence check failed", exc_info=True)
    return 0.0, None, None


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


def _resolve_symbol_filter(
    settings: BotSettings,
    symbol: str,
    key: str,
    default: float,
) -> float:
    """Return per-asset filter override when configured under [bot.assets.SYMBOL]."""
    assets = getattr(settings, "assets", None) or {}
    asset_cfg = assets.get(str(symbol or "").upper())
    if asset_cfg is None:
        return default
    overrides = getattr(asset_cfg, "filter_overrides", None) or {}
    if not isinstance(overrides, dict):
        return default
    raw = overrides.get(key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


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
        except (TypeError, ValueError):
            pct_1h_values.append(None)
    context_age_seconds = getattr(prepared, "context_snapshot_age_seconds", None)
    try:
        context_age = float(context_age_seconds) if context_age_seconds is not None else None
    except (TypeError, ValueError):
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
        except (TypeError, ValueError):
            pct_1h = 0.0
        try:
            pct_4h = float(payload.get("pct_4h") or 0.0)
        except (TypeError, ValueError):
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
    regime = effective_market_regime(
        str(getattr(prepared, "market_regime", "") or ""),
        bias_4h=str(getattr(prepared, "bias_4h", "") or "neutral"),
        price=getattr(prepared, "mark_price", None)
        or getattr(prepared.universe, "last_price", None),
        poc=getattr(prepared, "poc_1h", None),
    )
    btc_bias = str(getattr(prepared, "btc_bias", None) or signal.btc_bias or "neutral").lower()
    bias_4h = str(getattr(prepared, "bias_4h", "") or "neutral").lower()
    bias_1h = str(getattr(prepared, "bias_1h", prepared.bias_4h) or "neutral").lower()
    btc_phase = str(getattr(prepared, "btc_phase", "") or "").lower()
    trend_family = family in {"continuation", "breakout", "trend_follow"} or profile in {
        "trend_follow",
        "breakout_acceptance",
    }
    if trend_family and bias_4h == "uptrend" and bias_1h == "downtrend":
        return True, None, {"regime_gate": "htf_pullback_long_allowed"}
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
        _bias_1h_chk = str(getattr(prepared, "bias_1h", prepared.bias_4h) or "neutral").lower()
        if signal.setup_id in {"volume_climax_reversal", "cvd_divergence"}:
            _prep_regime = str(getattr(prepared, "market_regime", "") or "").lower()
            if _prep_regime == "trending" and _bias_1h_chk == "downtrend":
                reject_code = (
                    "volume_climax_trend_regime_blocked"
                    if signal.setup_id == "volume_climax_reversal"
                    else "cvd_trend_regime_blocked"
                )
                return (
                    False,
                    reject_code,
                    {
                        "regime_gate": "reversal_long_blocked_trending",
                        "market_regime": _prep_regime,
                        "bias_1h": _bias_1h_chk,
                        "setup_id": signal.setup_id,
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
    hard_bear_phase = btc_phase in {"decline", "distribution"}
    if bear_regime and bear_bias and trend_family and hard_bear_phase:
        return False, "regime_bear_long_blocked", details
    return True, None, details


def _regime_short_gate(
    signal: Signal,
    prepared: PreparedSymbol,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Block trend/orderbook shorts in confirmed bull context unless reversal setup."""
    direction = str(signal.direction or "").lower()
    if direction != "short":
        return True, None, {"regime_gate": "not_short"}

    symbol = str(signal.symbol or "").upper()
    if symbol not in {"BTCUSDT", "ETHUSDT", "SOLUSDT"}:
        _btc_bias_alt = str(
            getattr(prepared, "btc_bias", None) or signal.btc_bias or "neutral"
        ).lower()
        if _btc_bias_alt in {"uptrend", "bull"}:
            _ctx = getattr(prepared, "benchmark_context", {}) or {}
            _btc_payload = _ctx.get("BTCUSDT") if isinstance(_ctx, dict) else None
            _btc_pct_1h = 0.0
            if isinstance(_btc_payload, dict):
                try:
                    _btc_pct_1h = float(_btc_payload.get("pct_1h") or 0.0)
                except (TypeError, ValueError):
                    _btc_pct_1h = 0.0
            if _btc_pct_1h >= 0.006:
                _btc_corr = getattr(prepared, "btc_corr_1h", None)
                try:
                    _btc_corr_f = float(_btc_corr) if _btc_corr is not None else None
                except (TypeError, ValueError):
                    _btc_corr_f = None
                if _btc_corr_f is not None and abs(_btc_corr_f) < 0.7:
                    return (
                        True,
                        None,
                        {
                            "regime_gate": "btc_block_skipped_low_corr",
                            "btc_corr_1h": _btc_corr_f,
                            "btc_bias": _btc_bias_alt,
                            "symbol": symbol,
                        },
                    )
                return (
                    False,
                    "btc_uptrend_alt_short_blocked",
                    {
                        "regime_gate": "alt_short_vs_btc_uptrend",
                        "btc_bias": _btc_bias_alt,
                        "btc_pct_1h": _btc_pct_1h,
                        "symbol": symbol,
                    },
                )

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
        # Reversal shorts are NOT unconditionally exempt in a confirmed bull regime.
        # In markup/bull + uptrend bias, counter-trend shorts have >80% SL rate (empirical).
        # Only allow when btc_phase signals distribution or transition
        # (genuine reversal opportunity).
        _regime_chk = str(getattr(prepared, "market_regime", "") or "").lower()
        _btc_bias_chk = str(
            getattr(prepared, "btc_bias", None) or signal.btc_bias or "neutral"
        ).lower()
        _bias_4h_chk = str(getattr(prepared, "bias_4h", "") or "neutral").lower()
        _btc_phase_chk = str(getattr(prepared, "btc_phase", "") or "").lower()
        _bull_regime_chk = _regime_chk in {"bull", "markup", "risk_on"}
        _bull_bias_chk = _btc_bias_chk in {"uptrend", "bull"} or _bias_4h_chk == "uptrend"
        _distribution_phase = _btc_phase_chk in {"distribution", "transition", "decline"}
        if _bull_regime_chk and _bull_bias_chk and not _distribution_phase:
            return (
                False,
                "regime_bull_reversal_short_blocked",
                {
                    "regime_gate": "reversal_short_blocked_strong_bull",
                    "market_regime": _regime_chk,
                    "btc_bias": _btc_bias_chk,
                    "bias_4h": _bias_4h_chk,
                    "btc_phase": _btc_phase_chk,
                },
            )
        st15 = _latest_frame_float(prepared.work_15m, "supertrend_dir")
        st1h = _latest_frame_float(prepared.work_1h, "supertrend_dir")
        if st15 is not None and st1h is not None and st15 > 0.0 and st1h > 0.0:
            return (
                False,
                "supertrend_up_reversal_short_blocked",
                {
                    "regime_gate": "reversal_short_blocked_micro_up",
                    "supertrend_dir_15m": st15,
                    "supertrend_dir_1h": st1h,
                    "market_regime": _regime_chk,
                },
            )
        _bias_1h_chk = str(getattr(prepared, "bias_1h", prepared.bias_4h) or "neutral").lower()
        if signal.setup_id in {"volume_climax_reversal", "cvd_divergence"}:
            _prep_regime = str(getattr(prepared, "market_regime", "") or "").lower()
            if _prep_regime == "trending" and _bias_1h_chk == "uptrend":
                reject_code = (
                    "volume_climax_trend_regime_blocked"
                    if signal.setup_id == "volume_climax_reversal"
                    else "cvd_trend_regime_blocked"
                )
                return (
                    False,
                    reject_code,
                    {
                        "regime_gate": "reversal_short_blocked_trending",
                        "market_regime": _prep_regime,
                        "bias_1h": _bias_1h_chk,
                        "setup_id": signal.setup_id,
                    },
                )
        return True, None, {"regime_gate": "reversal_exempt"}

    regime = effective_market_regime(
        str(getattr(prepared, "market_regime", "") or ""),
        bias_4h=str(getattr(prepared, "bias_4h", "") or "neutral"),
        price=getattr(prepared, "mark_price", None)
        or getattr(prepared.universe, "last_price", None),
        poc=getattr(prepared, "poc_1h", None),
    )
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
    except (IndexError, TypeError, ValueError):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
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
    target_rr: tuple[float, float, float] | None = None,
    settings: BotSettings | None = None,
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
    rr_ladder = target_rr or resolve_target_rr(settings)
    tp3_rr = float(rr_ladder[2])
    reasons = (
        *signal.reasons,
        f"min_stop_normalized={min_stop_distance_pct:.2f}% rr_floor={rr_floor:.2f}",
    )

    if signal.direction == "long":
        stop = entry - min_risk
        tp1 = max(float(signal.take_profit_1), entry + min_risk * rr_floor)
        tp2 = max(float(signal.take_profit_2), tp1, entry + min_risk * tp2_rr)
        tp3 = max(float(signal.tp3), tp2, entry + min_risk * tp3_rr)
    else:
        stop = entry + min_risk
        tp1 = min(float(signal.take_profit_1), entry - min_risk * rr_floor)
        tp2 = min(float(signal.take_profit_2), tp1, entry - min_risk * tp2_rr)
        tp3 = min(float(signal.tp3), tp2, entry - min_risk * tp3_rr)

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
        target_rr=rr_ladder,
        settings=settings,
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
    """Reject when price has blown through the limit entry zone against the signal.

    For limit orders the entry is a structural level (support for longs,
    resistance for shorts).  Price drifting away on the *intended* side is
    normal — we are waiting for the fill.  Only reject when price has moved
    *against* the direction past the entry level by more than NxATR, which
    means the structural level was violated and the setup is invalidated.

    LONG : entry is below current price (support).  Reject if current_price
           has fallen more than threshold *below* entry_price (support broken).
    SHORT: entry is above current price (resistance).  Reject if current_price
           has risen more than threshold *above* entry_price (resistance broken).
    """
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

    atr_mult = float(getattr(settings.filters, "max_entry_deviation_atr_mult", 1.2))
    threshold = max(0.001, atr_mult * atr_pct / 100.0)

    direction = str(signal.direction or "").lower()
    cur = float(current_price)

    # Directional overshoot: how far price has moved *against* the signal past entry
    if direction == "long":
        # Support broken: price fell below entry
        adverse_deviation = (entry_price - cur) / entry_price
    else:
        # Resistance broken: price rose above entry
        adverse_deviation = (cur - entry_price) / entry_price

    details: dict[str, Any] = {
        "entry_price": entry_price,
        "current_price": cur,
        "adverse_deviation_pct": round(adverse_deviation * 100.0, 4),
        "atr_pct": atr_pct,
        "max_deviation_pct": round(threshold * 100.0, 4),
        "atr_mult": atr_mult,
        "direction": direction,
    }
    if adverse_deviation > threshold:
        return False, "entry_staleness", details
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

    base_min = _resolve_symbol_filter(
        settings,
        prepared.symbol,
        "min_atr_pct",
        float(settings.filters.min_atr_pct),
    )
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

    age_ok, age_reason = _signal_age_gate(signal, settings, primary_timeframe=primary_timeframe)
    if not age_ok:
        return _reject(age_reason or "signal_too_old", base)

    htf_delta, htf_note, htf_reject = _htf_zone_confluence_adjustment(signal, prepared, settings)
    if htf_reject:
        return _reject(htf_reject, base)
    if htf_delta > 0.0:
        base = replace(base, score=min(1.0, base.score + htf_delta))
        passed.append(htf_note or "htf_zone_overlap_bonus")

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
    if filter_stage_enabled(settings, "mark_deviation"):
        if prepared.mark_price is None or prepared.mark_price <= 0:
            return _reject(
                "mark_price_unavailable",
                base,
                details={"mark_price": prepared.mark_price},
            )
        if prepared.ticker_price is None or prepared.ticker_price <= 0:
            return _reject(
                "ticker_price_unavailable",
                base,
                details={"ticker_price": prepared.ticker_price},
            )
        ticker_age = prepared.ticker_price_age_seconds or 0.0
        mark_price_details: dict[str, Any] = {
            "mark_price": prepared.mark_price,
            "comparison_price": prepared.ticker_price,
            "comparison_source": "ws_ticker",
            "comparison_age_seconds": ticker_age,
            "mark_price_age_seconds": prepared.mark_price_age_seconds,
        }
        if ticker_age > 60.0:
            # Stale ticker: skip deviation check — comparing mark price against a
            # 60s+ old ticker produces false rejects when markets moved normally.
            mark_price_details["deviation_skipped"] = "ticker_stale"
            passed.append("mark_price_ok_ticker_stale")
        else:
            deviation = abs(prepared.mark_price - prepared.ticker_price) / prepared.ticker_price
            mark_price_details["deviation_pct"] = deviation
            if deviation > settings.filters.max_mark_price_deviation_pct:
                return _reject("mark_price_deviation", base, details=mark_price_details)
            passed.append("mark_price_ok")
    else:
        passed.append("mark_deviation_stage_disabled")

    # --- 3. Spread ---
    if filter_stage_enabled(settings, "spread"):
        if prepared.spread_bps is None:
            return _reject("spread_unavailable", base)
        max_spread = _tier_max_spread_bps(prepared, settings)
        if prepared.spread_bps > max_spread:
            return _reject("spread_too_wide", base)
        if _liquidity_tier(prepared, settings) == "radar":
            passed.append("radar_tier_spread_ok")
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
        max_atr_pct = _resolve_symbol_filter(
            settings,
            prepared.symbol,
            "max_atr_pct",
            float(settings.filters.max_atr_pct),
        )
        if atr_pct > max_atr_pct:
            return _reject("atr_too_high", replace(base, atr_pct=atr_pct))
        passed.append("atr_ok")
    else:
        passed.append("atr_stage_disabled")
        atr_frame = primary_frame if primary_frame is not None else prepared.work_15m
        if not atr_frame.is_empty() and "atr_pct" in atr_frame.columns:
            atr_pct = float(atr_frame.item(-1, "atr_pct") or 0.0)
        else:
            atr_pct = 0.0

    # --- 4b. ADX policy (setup/family aware) ---
    adx_1h = 0.0
    if not prepared.work_1h.is_empty() and "adx14" in prepared.work_1h.columns:
        adx_1h = float(prepared.work_1h.item(-1, "adx14") or 0.0)
    setup_overrides = settings.filters.setups.get(signal.setup_id, {})
    if not isinstance(setup_overrides, dict):
        setup_overrides = {}
    default_min_adx = _resolve_symbol_filter(
        settings,
        prepared.symbol,
        "min_adx_1h",
        float(settings.filters.min_adx_1h),
    )
    min_adx_1h = float(setup_overrides.get("min_adx_1h", default_min_adx))
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
    # answers50 P1: ADX<20 on trend families → soft score penalty, not hard reject.
    _trend_families = {"trend_follow", "continuation", "breakout", "momentum"}
    adx_ranging_absolute_penalty = False
    if (
        adx_1h > 0.0
        and adx_1h < _COMPRESSION_ADX_CEILING
        and signal.strategy_family in _trend_families
        and not deep_analysis_asset
    ):
        adx_ranging_absolute_penalty = True
        passed.append("adx_ranging_absolute_penalized")
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
    short_downtrend_penalty_applied = False
    short_downtrend_penalty_factor = float(
        setup_overrides.get("short_downtrend_penalty_factor", 0.88)
    )
    funding_short_penalty_applied = False
    funding_short_penalty_factor = float(setup_overrides.get("funding_short_penalty_factor", 0.90))
    if trend_conflict:
        trend_details = {
            "signal_direction": signal.direction,
            "dominant_1h": dominant_1h,
            "setup_id": signal.setup_id,
            "strategy_family": signal.strategy_family,
            "confirmation_profile": signal.confirmation_profile,
        }
        trend_conflict_penalty_applied = True
        passed.append("trend_conflict_1h_penalized")
    else:
        passed.append("trend_context_ok")

    if (
        getattr(settings.filters, "trend_conflict_soft", True)
        and signal.direction == "long"
        and btc_phase in {"decline", "distribution"}
    ):
        btc_decline_penalty_applied = True
        passed.append("btc_decline_penalty_eligible")
    elif btc_phase:
        passed.append("btc_phase_ok")

    if (
        signal.direction == "short"
        and dominant_1h == "downtrend"
        and signal.strategy_family in {"continuation", "breakout", "trend_follow"}
    ):
        short_downtrend_penalty_applied = True
        passed.append("short_downtrend_penalty_eligible")
    else:
        passed.append("short_downtrend_context_ok")

    bias_4h = str(getattr(prepared, "bias_4h", "") or "neutral").lower()
    atr_pct = _optional_float(getattr(prepared, "atr_pct", None)) or _optional_float(
        getattr(signal, "atr_pct", None)
    )
    if (
        signal.direction == "short"
        and bias_4h == "downtrend"
        and atr_pct is not None
        and atr_pct > 1.5
    ):
        return _reject(
            "short_downtrend_high_atr",
            base,
            details={
                "bias_4h": bias_4h,
                "atr_pct": atr_pct,
                "threshold_atr_pct": 1.5,
                "signal_direction": signal.direction,
            },
        )

    funding_rate = _optional_float(getattr(prepared, "funding_rate", None))
    funding_moderate = float(getattr(settings.scoring, "funding_rate_moderate", 0.0005) or 0.0005)
    funding_extreme = float(getattr(settings.scoring, "funding_rate_extreme", 0.0010) or 0.0010)
    if funding_rate is not None:
        _fr_abs = abs(funding_rate)
        _fr_details = {
            "funding_rate": funding_rate,
            "funding_extreme": funding_extreme,
            "signal_direction": signal.direction,
        }
        # Hard gate: extreme crowding on the same side as the signal.
        # Positive funding > extreme → crowded longs → block new longs.
        # Negative funding < -extreme → crowded shorts → block new shorts.
        if signal.direction == "long" and funding_rate > funding_extreme:
            return _reject("funding_crowded_longs", base, details=_fr_details)
        if signal.direction == "short" and funding_rate < -funding_extreme:
            return _reject("funding_crowded_shorts", base, details=_fr_details)
        # Legacy: positive funding headwinds shorts (short squeeze risk).
        if signal.direction == "short" and funding_rate > funding_extreme:
            return _reject("funding_headwind_short", base, details=_fr_details)
        # Moderate penalty zone: applies score penalty later in pipeline.
        if signal.direction == "long" and funding_rate > funding_moderate:
            funding_short_penalty_applied = True  # reuse flag; penalty applied below
            passed.append("funding_long_penalty_eligible")
        elif signal.direction == "short" and funding_rate > funding_moderate:
            funding_short_penalty_applied = True
            passed.append("funding_short_penalty_eligible")
        else:
            passed.append("funding_context_ok")
    else:
        passed.append("funding_context_ok")

    # OI divergence filter: rising price + falling OI = short-covering rally, not real breakout.
    # Applied only to breakout/continuation/momentum families where OI confirmation matters.
    _oi_breakout_families = {"breakout", "continuation", "momentum", "trend_follow"}
    if signal.strategy_family in _oi_breakout_families:
        oi_chg = prepared.oi_change_pct
        if oi_chg is not None:
            _oi_price_up = signal.direction == "long"
            _oi_price_dn = signal.direction == "short"
            _oi_falling = oi_chg < -0.03  # OI dropped >3% → participation leaving
            _oi_rising = oi_chg > 0.03  # OI grew >3% → new money entering
            if _oi_price_up and _oi_falling:
                # Rising price + falling OI = weak breakout; downgrade, don't hard reject
                # so confluence scoring can still pass high-quality setups.
                passed.append(f"oi_divergence_weak_breakout|oi_chg={oi_chg:.3f}")
            elif _oi_price_dn and _oi_rising:
                passed.append(f"oi_divergence_weak_breakdown|oi_chg={oi_chg:.3f}")
            elif (_oi_price_up and _oi_rising) or (_oi_price_dn and _oi_falling):
                passed.append(f"oi_confirming|oi_chg={oi_chg:.3f}")
            else:
                passed.append(f"oi_neutral|oi_chg={oi_chg:.3f}")
        else:
            passed.append("oi_unavailable")

    # Premium/Discount zone (SMC/ICT): shorts in discount = hard-block (answers50 Q32).
    # Longs in premium = soft penalty for trend/continuation families only.
    _pd_families = {
        "trend_follow",
        "continuation",
        "breakout",
        "momentum",
        "orderbook",
        "orderflow",
    }
    if not prepared.work_1h.is_empty():
        _w1h = prepared.work_1h
        _lookback = min(50, _w1h.height)
        _tail = _w1h.tail(_lookback)
        try:
            _range_high = float(_tail["high"].max())
            _range_low = float(_tail["low"].min())
            _equilibrium = (_range_high + _range_low) / 2.0
            _price = prepared.mark_price or signal.entry_mid
            if _range_high > _range_low and _price is not None and _price > 0:
                _in_discount = _price <= _equilibrium
                _in_premium = _price >= _equilibrium
                if signal.direction == "short" and _in_discount:
                    return _reject(
                        "pd_zone_short_in_discount",
                        base,
                        details={
                            "equilibrium": _equilibrium,
                            "price": _price,
                            "setup_id": signal.setup_id,
                        },
                    )
                if signal.strategy_family in _pd_families:
                    if signal.direction == "long" and _in_premium:
                        passed.append(
                            f"pd_zone_mismatch_long_in_premium|eq={_equilibrium:.4f}|price={_price:.4f}"
                        )
                    else:
                        passed.append(
                            f"pd_zone_ok|{'discount' if _in_discount else 'premium'}|eq={_equilibrium:.4f}"
                        )
        except Exception:  # noqa: BLE001
            passed.append("pd_zone_unavailable")

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

    compression_regime = _is_compression_regime(prepared, adx_1h=adx_1h)
    if compression_regime:
        passed.append("compression_regime_detected")
        if signal.direction == "short" and signal.setup_id in _COMPRESSION_SHORT_BLOCK_SETUPS:
            return _reject(
                "compression_short_blocked",
                base,
                details={
                    "setup_id": signal.setup_id,
                    "symbol": signal.symbol,
                    "adx_1h": adx_1h,
                },
            )

    # --- 5. Stop distance ---
    if filter_stage_enabled(settings, "stop"):
        min_stop_distance_pct = _effective_min_stop_distance_pct(settings, atr_pct)
        effective_min_rr = float(setup_overrides.get("min_rr", settings.filters.min_risk_reward))
        updated, stop_expanded = _expand_signal_to_min_stop(
            updated,
            min_stop_distance_pct=min_stop_distance_pct,
            min_rr=effective_min_rr,
            settings=settings,
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

    if adx_ranging_absolute_penalty:
        pre_penalty_score = updated.score
        adjusted_score = max(0.0, pre_penalty_score - 0.10)
        updated = replace(updated, score=adjusted_score)
        penalty_delta = round(adjusted_score - pre_penalty_score, 6)
        if scoring_result is not None:
            scoring_result = replace(
                scoring_result,
                final_score=adjusted_score,
                adjustments={
                    **scoring_result.adjustments,
                    "adx_ranging_absolute_penalty": penalty_delta,
                },
            )
        updated = replace(
            updated,
            passed_filters=(*updated.passed_filters, "adx_ranging_absolute_penalty_applied"),
        )

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

    if short_downtrend_penalty_applied:
        pre_penalty_score = updated.score
        adjusted_score = pre_penalty_score * short_downtrend_penalty_factor
        updated = replace(updated, score=adjusted_score)
        penalty_delta = round(adjusted_score - pre_penalty_score, 6)
        if scoring_result is not None:
            scoring_result = replace(
                scoring_result,
                final_score=adjusted_score,
                adjustments={
                    **scoring_result.adjustments,
                    "short_downtrend_penalty": penalty_delta,
                },
            )
        updated = replace(
            updated,
            passed_filters=(*updated.passed_filters, "short_downtrend_penalty_applied"),
        )

    if funding_short_penalty_applied:
        pre_penalty_score = updated.score
        adjusted_score = pre_penalty_score * funding_short_penalty_factor
        updated = replace(updated, score=adjusted_score)
        penalty_delta = round(adjusted_score - pre_penalty_score, 6)
        if scoring_result is not None:
            scoring_result = replace(
                scoring_result,
                final_score=adjusted_score,
                adjustments={
                    **scoring_result.adjustments,
                    "funding_short_penalty": penalty_delta,
                },
            )
        updated = replace(
            updated,
            passed_filters=(*updated.passed_filters, "funding_short_penalty_applied"),
        )

    # --- 8b. Strategy SL-rate feedback penalty (п.26) ---
    sl_rate = _STRATEGY_SL_RATES.get(signal.setup_id, 0.0)
    min_samples = int(getattr(settings.delivery, "min_sl_penalty_samples", 10) or 10)
    sample_count = int(_STRATEGY_SL_SAMPLE_COUNTS.get(signal.setup_id, min_samples))
    if sl_rate > _SL_PENALTY_THRESHOLD and sample_count >= min_samples:
        excess = (sl_rate - _SL_PENALTY_THRESHOLD) / (1.0 - _SL_PENALTY_THRESHOLD)
        sl_mult = max(_SL_PENALTY_MAX_MULT, 1.0 - excess * (1.0 - _SL_PENALTY_MAX_MULT))
        sl_adjusted = updated.score * sl_mult
        updated = replace(updated, score=sl_adjusted)
        updated = replace(
            updated,
            passed_filters=(
                *updated.passed_filters,
                f"strategy_sl_penalty:{sl_rate:.2f}x{sl_mult:.2f}",
            ),
        )
        LOGGER.debug(
            "strategy sl-rate penalty | setup=%s sl_rate=%.2f mult=%.2f score=%.3f→%.3f",
            signal.setup_id,
            sl_rate,
            sl_mult,
            updated.score / sl_mult,
            updated.score,
        )

    # --- 9. Minimum score gate (final gate after ALL adjustments) ---
    if filter_stage_enabled(settings, "min_score"):
        effective_min_score = float(settings.filters.min_score)
        if deep_analysis_asset:
            deep_score_floor = 0.48 if primary_timeframe in {"1h", "4h"} else 0.50
            if signal.setup_id in {"bos_choch", "liquidation_heatmap"}:
                deep_score_floor = 0.40
            effective_min_score = min(effective_min_score, deep_score_floor)
        _regime = str(getattr(prepared, "market_regime", "") or "").lower()
        _btc_bias = str(getattr(prepared, "btc_bias", None) or signal.btc_bias or "neutral").lower()
        _bias_4h = str(getattr(prepared, "bias_4h", "") or "neutral").lower()
        if (
            str(signal.direction or "").lower() == "short"
            and (_regime in {"bull", "markup", "risk_on"} or _btc_bias in {"uptrend", "bull"})
            and _bias_4h == "uptrend"
        ):
            effective_min_score = min(1.0, effective_min_score + 0.05)
        if (
            str(signal.direction or "").lower() == "long"
            and (_regime in {"bear", "markdown", "risk_off"} or _btc_bias in {"downtrend", "bear"})
            and _bias_4h == "downtrend"
        ):
            effective_min_score = min(1.0, effective_min_score + 0.05)
        if _liquidity_tier(prepared, settings) == "radar":
            effective_min_score = min(
                1.0,
                effective_min_score + float(settings.universe.radar_min_score_delta),
            )
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

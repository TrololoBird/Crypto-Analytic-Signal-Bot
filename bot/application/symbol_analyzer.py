from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from bot.core.engine import StrategyDecision
from bot.core.runtime_errors import build_runtime_error_payload, classify_runtime_error
from bot.features import min_required_bars, prepare_symbol
from bot.filters import apply_global_filters
from bot.market_data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.models import (
    PipelineResult,
    PreparedSymbol,
    Signal,
    SymbolFrames,
    UniverseSymbol,
)
from bot.runtime_policy import is_deep_analysis_symbol

if TYPE_CHECKING:
    from bot.application.bot import SignalBot


LOG = logging.getLogger("bot.application.bot")
_DEGRADATION_ERRORS = (
    MarketDataUnavailable,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
)
_DEFAULT_HISTORY_FETCH_LIMIT = 300
_HISTORY_FETCH_BUFFER_BARS = 60


def _history_fetch_limit(minimums: dict[str, int], interval: str) -> int:
    required = int(minimums.get(interval, 0))
    baseline = (
        _DEFAULT_HISTORY_FETCH_LIMIT if interval in {"5m", "15m", "1h", "4h"} else 240
    )
    return max(baseline, required + _HISTORY_FETCH_BUFFER_BARS)


def _attach_rejection_rollups(
    funnel: dict[str, Any],
    rejected: list[dict[str, Any]],
) -> None:
    """Attach stage/setup reason rollups to the symbol funnel."""
    by_stage: Counter[str] = Counter()
    by_setup: Counter[str] = Counter()
    by_stage_reason: Counter[str] = Counter()
    by_setup_reason: Counter[str] = Counter()
    for row in rejected:
        stage = str(row.get("stage") or "unknown")
        setup_id = str(row.get("setup_id") or "unknown")
        reason = str(row.get("reason") or "unknown")
        by_stage[stage] += 1
        by_setup[setup_id] += 1
        by_stage_reason[f"{stage}:{reason}"] += 1
        by_setup_reason[f"{setup_id}:{reason}"] += 1
    funnel["rejects_by_stage"] = dict(by_stage)
    funnel["rejects_by_setup"] = dict(by_setup)
    funnel["reject_reasons_by_stage"] = dict(by_stage_reason)
    funnel["reject_reasons_by_setup"] = dict(by_setup_reason)


def _apply_setup_score_adjustment(
    signal: Signal, score_adjustment: float
) -> tuple[Signal, dict[str, Any]]:
    """Apply adaptive setup scoring without converting mild penalties into hard blocks."""
    try:
        adjustment = float(score_adjustment)
    except (TypeError, ValueError):
        adjustment = 0.0
    if not adjustment:
        return signal, {"applied": False, "adjustment": 0.0}

    adjusted_score = round(min(1.0, max(0.0, float(signal.score) + adjustment)), 4)
    if adjusted_score == signal.score:
        return signal, {"applied": False, "adjustment": adjustment}

    reason = (
        "setup_performance_bonus" if adjustment > 0 else "setup_performance_penalty"
    )
    reasons = signal.reasons if reason in signal.reasons else (*signal.reasons, reason)
    return (
        replace(signal, score=adjusted_score, reasons=reasons),
        {
            "applied": True,
            "adjustment": adjustment,
            "score_before": signal.score,
            "score_after": adjusted_score,
            "reason": reason,
        },
    )


class SymbolAnalyzer:
    def __init__(self, bot: SignalBot) -> None:
        self._bot = bot

    def _minimums(self) -> dict[str, int]:
        config_min_1h = int(self._bot.settings.filters.min_bars_1h)
        registry = getattr(self._bot, "_modern_registry", None)
        enabled_strategies = (
            registry.get_enabled()
            if registry is not None and hasattr(registry, "get_enabled")
            else ()
        )
        strategies_min_1h = max(
            (
                int(getattr(strategy.metadata, "min_history_bars", 0) or 0)
                for strategy in enabled_strategies
            ),
            default=30,
        )
        return min_required_bars(
            min_bars_15m=self._bot.settings.filters.min_bars_15m,
            min_bars_1h=max(config_min_1h, strategies_min_1h),
            min_bars_4h=self._bot.settings.filters.min_bars_4h,
        )

    @staticmethod
    def _degrade_event(
        *,
        symbol: str,
        stage: str,
        source: str,
        reason: str,
        fallback_used: str,
        exception_type: str | None = None,
    ) -> dict[str, Any]:
        return {
            "degraded": True,
            "degrade_reason": f"{stage}:{reason}",
            "fallback_used": fallback_used,
            "degrade_symbol": symbol,
            "degrade_stage": stage,
            "degrade_source": source,
            "exception_type": exception_type,
        }

    @staticmethod
    def _log_degradation(
        *,
        level: int,
        symbol: str,
        stage: str,
        source: str,
        reason: str,
        fallback_used: str,
        exception_type: str | None = None,
    ) -> None:
        LOG.log(
            level,
            "enrichment degraded | symbol=%s stage=%s source=%s reason=%s fallback_used=%s exception_type=%s",
            symbol,
            stage,
            source,
            reason,
            fallback_used,
            exception_type,
        )

    @staticmethod
    def _frame_float(frame: Any, column: str) -> float | None:
        if frame is None or getattr(frame, "is_empty", lambda: True)():
            return None
        if column not in getattr(frame, "columns", []):
            return None
        try:
            value = frame.item(-1, column)
        except (IndexError, TypeError, ValueError):
            return None
        try:
            if value is None:
                return None
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return (
            numeric
            if numeric == numeric and numeric not in (float("inf"), float("-inf"))
            else None
        )

    @staticmethod
    def _crowding_flags(prepared: PreparedSymbol, direction: str) -> dict[str, Any]:
        flags = set(getattr(prepared, "data_freshness_flags", ()) or ())
        if "crowding_context_missing" in flags:
            return {
                "available": False,
                "exhaustion": False,
                "trend_support": False,
                "headwind": False,
            }

        top_account = prepared.top_account_ls_ratio or prepared.ls_ratio
        top_position = prepared.top_position_ls_ratio
        global_ratio = prepared.global_account_ls_ratio or prepared.global_ls_ratio
        gap = prepared.top_vs_global_ls_gap

        if direction == "long":
            exhaustion = bool(
                (global_ratio is not None and global_ratio <= 0.9)
                or (top_account is not None and top_account <= 0.88)
                or (top_position is not None and top_position <= 0.9)
                or (gap is not None and gap <= -0.1)
            )
            trend_support = bool(
                (
                    (top_position is not None and 1.02 <= top_position <= 1.35)
                    or (top_account is not None and 1.0 <= top_account <= 1.3)
                )
                and not exhaustion
                and not (gap is not None and gap >= 0.22)
            )
            headwind = bool(
                (top_account is not None and top_account >= 1.7)
                or (top_position is not None and top_position >= 1.75)
                or (gap is not None and gap >= 0.22)
            )
        else:
            exhaustion = bool(
                (global_ratio is not None and global_ratio >= 1.1)
                or (top_account is not None and top_account >= 1.12)
                or (top_position is not None and top_position >= 1.1)
                or (gap is not None and gap >= 0.1)
            )
            trend_support = bool(
                (
                    (top_position is not None and 0.7 <= top_position <= 0.98)
                    or (top_account is not None and 0.78 <= top_account <= 1.0)
                )
                and not exhaustion
                and not (gap is not None and gap <= -0.22)
            )
            headwind = bool(
                (top_account is not None and top_account <= 0.62)
                or (top_position is not None and top_position <= 0.58)
                or (gap is not None and gap <= -0.22)
            )
        return {
            "available": any(
                value is not None
                for value in (top_account, top_position, global_ratio, gap)
            ),
            "exhaustion": exhaustion,
            "trend_support": trend_support,
            "headwind": headwind,
            "top_account_ls_ratio": top_account,
            "top_position_ls_ratio": top_position,
            "global_account_ls_ratio": global_ratio,
            "top_vs_global_ls_gap": gap,
        }

    def directional_context(
        self, signal: Signal, prepared: PreparedSymbol
    ) -> dict[str, Any]:
        work_5m = prepared.work_5m
        close_5m = self._frame_float(work_5m, "close")
        ema20_5m = self._frame_float(work_5m, "ema20")
        supertrend_5m = self._frame_float(work_5m, "supertrend_dir")
        delta_ratio_5m = self._frame_float(work_5m, "delta_ratio")
        taker_ratio = prepared.taker_ratio
        flow_proxy = None
        if prepared.agg_trade_delta_30s is not None:
            flow_proxy = float(prepared.agg_trade_delta_30s)
        elif taker_ratio is not None:
            flow_proxy = float(taker_ratio) - 1.0
        elif delta_ratio_5m is not None:
            flow_proxy = float(delta_ratio_5m) - 0.5

        premium_velocity = prepared.premium_slope_5m
        if premium_velocity is None:
            premium_velocity = prepared.mark_index_spread_bps
        depth_imbalance = prepared.depth_imbalance
        microprice_bias = prepared.microprice_bias
        crowding = self._crowding_flags(prepared, signal.direction)

        direction = signal.direction
        if direction == "long":
            trend_confirms = bool(
                close_5m is not None
                and ema20_5m is not None
                and close_5m >= ema20_5m
                and (supertrend_5m is None or supertrend_5m >= 0.0)
            )
            flow_confirms = bool(
                (flow_proxy is not None and flow_proxy >= 0.03)
                or (delta_ratio_5m is not None and delta_ratio_5m >= 0.53)
            )
            premium_confirms = bool(
                (premium_velocity is not None and premium_velocity >= 0.0)
                or (
                    prepared.mark_index_spread_bps is not None
                    and prepared.mark_index_spread_bps >= -4.0
                )
            )
            depth_confirms = bool(
                (depth_imbalance is not None and depth_imbalance >= 0.05)
                or (microprice_bias is not None and microprice_bias >= 0.0)
            )
            premium_exhaustion = bool(
                (
                    prepared.premium_zscore_5m is not None
                    and prepared.premium_zscore_5m <= -1.5
                )
                or (
                    prepared.mark_index_spread_bps is not None
                    and prepared.mark_index_spread_bps <= -8.0
                )
            )
            crowd_exhaustion = bool(crowding["exhaustion"])
            aggressor_reversal = bool(
                prepared.aggression_shift is not None
                and prepared.aggression_shift >= 0.03
            )
            regime_opposes = (
                prepared.regime_1h_confirmed == "downtrend"
                or prepared.bias_1h == "downtrend"
            )
            flow_opposes = bool(flow_proxy is not None and flow_proxy <= -0.03)
        else:
            trend_confirms = bool(
                close_5m is not None
                and ema20_5m is not None
                and close_5m <= ema20_5m
                and (supertrend_5m is None or supertrend_5m <= 0.0)
            )
            flow_confirms = bool(
                (flow_proxy is not None and flow_proxy <= -0.03)
                or (delta_ratio_5m is not None and delta_ratio_5m <= 0.47)
            )
            premium_confirms = bool(
                (premium_velocity is not None and premium_velocity <= 0.0)
                or (
                    prepared.mark_index_spread_bps is not None
                    and prepared.mark_index_spread_bps <= 4.0
                )
            )
            depth_confirms = bool(
                (depth_imbalance is not None and depth_imbalance <= -0.05)
                or (microprice_bias is not None and microprice_bias <= 0.0)
            )
            premium_exhaustion = bool(
                (
                    prepared.premium_zscore_5m is not None
                    and prepared.premium_zscore_5m >= 1.5
                )
                or (
                    prepared.mark_index_spread_bps is not None
                    and prepared.mark_index_spread_bps >= 8.0
                )
            )
            crowd_exhaustion = bool(crowding["exhaustion"])
            aggressor_reversal = bool(
                prepared.aggression_shift is not None
                and prepared.aggression_shift <= -0.03
            )
            regime_opposes = (
                prepared.regime_1h_confirmed == "uptrend"
                or prepared.bias_1h == "uptrend"
            )
            flow_opposes = bool(flow_proxy is not None and flow_proxy >= 0.03)
        exhaustion_hits = {
            "premium_extreme": premium_exhaustion,
            "liquidation_imbalance": bool(
                prepared.liquidation_score is not None
                and prepared.liquidation_score <= -0.35
            ),
            "crowd_stretch": crowd_exhaustion,
            "aggressor_reversal": aggressor_reversal,
        }
        return {
            "used": work_5m is not None and not work_5m.is_empty(),
            "close_5m": close_5m,
            "ema20_5m": ema20_5m,
            "supertrend_dir_5m": supertrend_5m,
            "delta_ratio_5m": delta_ratio_5m,
            "flow_proxy": flow_proxy,
            "mark_index_spread_bps": prepared.mark_index_spread_bps,
            "premium_zscore_5m": prepared.premium_zscore_5m,
            "premium_slope_5m": prepared.premium_slope_5m,
            "depth_imbalance": prepared.depth_imbalance,
            "microprice_bias": prepared.microprice_bias,
            "regime_1h": prepared.regime_1h_confirmed,
            "bias_1h": prepared.bias_1h,
            "trend_confirms": trend_confirms,
            "flow_confirms": flow_confirms,
            "premium_confirms": premium_confirms,
            "depth_confirms": depth_confirms,
            "regime_opposes": regime_opposes,
            "flow_opposes": flow_opposes,
            "crowding": crowding,
            "crowd_trend_support": crowding["trend_support"],
            "crowd_headwind": crowding["headwind"],
            "exhaustion_hits": exhaustion_hits,
            "exhaustion_count": sum(1 for value in exhaustion_hits.values() if value),
        }

    def check_family_precheck(
        self,
        signal: Signal,
        prepared: PreparedSymbol,
        metadata: Any | None,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        details = self.directional_context(signal, prepared)
        family = getattr(metadata, "family", signal.strategy_family)
        profile = getattr(metadata, "confirmation_profile", signal.confirmation_profile)
        details["family"] = family
        details["confirmation_profile"] = profile
        strong_opposition = details["regime_opposes"] and details["flow_opposes"]
        if (
            family in {"continuation", "breakout"}
            and strong_opposition
            and details["exhaustion_count"] == 0
        ):
            details["soft_penalty_applied"] = True
            details["penalty_factor"] = 0.80
            details["penalty_reason"] = f"family_precheck_opposes_{signal.direction}"
            return True, None, details
        if (
            profile == "trend_follow"
            and details["flow_opposes"]
            and not details["trend_confirms"]
        ):
            return False, f"flow_precheck_opposes_{signal.direction}", details
        return True, None, details

    def apply_alignment_penalty(
        self,
        signal: Signal,
        prepared: PreparedSymbol,
        metadata: Any | None,
    ) -> tuple[Signal, dict[str, Any]]:
        family = getattr(metadata, "family", signal.strategy_family)
        profile = getattr(metadata, "confirmation_profile", signal.confirmation_profile)
        if signal.direction == "long":
            opposing_votes = int(prepared.regime_1h_confirmed == "downtrend") + int(
                prepared.bias_1h == "downtrend"
            )
        else:
            opposing_votes = int(prepared.regime_1h_confirmed == "uptrend") + int(
                prepared.bias_1h == "uptrend"
            )
        details = {
            "regime_1h": prepared.regime_1h_confirmed,
            "bias_1h": prepared.bias_1h,
            "opposing_votes": opposing_votes,
            "applied": False,
            "family": family,
            "confirmation_profile": profile,
        }
        if (
            opposing_votes == 0
            or family == "reversal"
            or profile == "countertrend_exhaustion"
        ):
            return signal, details
        if signal.score <= 0.0:
            details["skipped_reason"] = "non_positive_score"
            return signal, details
        penalty_factor = 0.98 if opposing_votes == 1 else 0.95
        reasons = (
            signal.reasons
            if "alignment_penalty" in signal.reasons
            else (*signal.reasons, "alignment_penalty")
        )
        details["applied"] = True
        details["penalty_factor"] = penalty_factor
        return replace(
            signal,
            score=round(max(signal.score * penalty_factor, 0.0), 4),
            reasons=reasons,
        ), details

    def check_family_confirmation(
        self,
        signal: Signal,
        prepared: PreparedSymbol,
        metadata: Any | None,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        details = self.directional_context(signal, prepared)
        family = getattr(metadata, "family", signal.strategy_family)
        profile = getattr(metadata, "confirmation_profile", signal.confirmation_profile)
        deep_analysis_asset = is_deep_analysis_symbol(prepared, self._bot.settings)
        primary_timeframe = str(getattr(prepared, "primary_timeframe", "15m") or "15m")
        details["family"] = family
        details["confirmation_profile"] = profile
        details["primary_timeframe"] = primary_timeframe
        if deep_analysis_asset:
            details["deep_analysis_policy"] = "soft_fast_context"
        if (
            not details["used"]
            and details["flow_proxy"] is None
            and prepared.mark_index_spread_bps is None
            and prepared.depth_imbalance is None
            and prepared.microprice_bias is None
        ):
            details["fallback"] = "context_missing"
            strict_data_quality = bool(
                getattr(self._bot.settings.runtime, "strict_data_quality", True)
            )
            if strict_data_quality and family in {"continuation", "breakout"}:
                if deep_analysis_asset and primary_timeframe in {"1h", "4h"}:
                    details["fallback"] = "deep_primary_without_fast_context"
                    return True, None, details
                details["fast_context_weak"] = True
                return True, None, details
            return True, None, details
        details["confirmation_votes"] = {
            "trend_5m": details["trend_confirms"],
            "flow_5m": details["flow_confirms"],
            "premium_slope": details["premium_confirms"],
            "depth_focus": details["depth_confirms"],
        }
        if details["crowding"]["available"]:
            details["confirmation_votes"]["crowding_support"] = details[
                "crowd_trend_support"
            ]
        details["confirmation_count"] = sum(
            1 for value in details["confirmation_votes"].values() if value
        )
        if family == "reversal" or profile == "countertrend_exhaustion":
            if details["exhaustion_count"] > 0:
                return True, None, details
            if details["regime_opposes"] and details["flow_opposes"]:
                return False, f"reversal_unconfirmed_{signal.direction}", details
            return True, None, details
        if (
            details["crowd_headwind"]
            and not details["crowd_trend_support"]
            and details["confirmation_count"] < 3
        ):
            if deep_analysis_asset and (
                primary_timeframe in {"1h", "4h"} or details["confirmation_count"] >= 1
            ):
                details["relaxed_reject"] = f"crowding_headwind_{signal.direction}"
                return True, None, details
            return False, f"crowding_headwind_{signal.direction}", details
        if (
            family == "breakout"
            and details["crowding"]["available"]
            and not details["crowd_trend_support"]
            and details["confirmation_count"] < 3
        ):
            if deep_analysis_asset and (
                primary_timeframe in {"1h", "4h"} or details["confirmation_count"] >= 1
            ):
                details["relaxed_reject"] = (
                    f"breakout_crowding_unconfirmed_{signal.direction}"
                )
                return True, None, details
            return False, f"breakout_crowding_unconfirmed_{signal.direction}", details
        if details["confirmation_count"] >= 2:
            return True, None, details
        if (
            details["regime_opposes"]
            and details["flow_opposes"]
            and details["exhaustion_count"] == 0
        ):
            return False, f"hard_context_opposes_{signal.direction}", details
        if deep_analysis_asset and (
            primary_timeframe in {"1h", "4h"} or details["confirmation_count"] >= 1
        ):
            details["relaxed_reject"] = f"5m_opposes_{signal.direction}"
            return True, None, details
        return False, f"5m_opposes_{signal.direction}", details

    async def run_modern_analysis(
        self,
        item: UniverseSymbol,
        frames: SymbolFrames,
        trigger: str = "modern_engine",
        event_ts: datetime | None = None,
        ws_enrichments: dict[str, Any] | None = None,
    ) -> PipelineResult:
        """Run modern SignalEngine analysis for a symbol.

        Replaces legacy SignalPipeline.process_symbol().

        Returns:
            PipelineResult compatible with legacy pipeline output
        """
        event_ts = event_ts or datetime.now(UTC)
        candidates: list[Signal] = []
        rejected: list[dict[str, Any]] = []
        prepared: PreparedSymbol | None = None
        funnel: dict[str, Any] = {
            "shortlist_entered": True,
            "frame_rows": {},
            "frame_readiness": {},
            "detector_runs": 0,
            "post_filter_candidates": 0,
            "raw_hits": 0,
            "raw_hits_by_setup": {},
            "strategy_rejects_by_setup": {},
            "family_precheck_rejects": 0,
            "alignment_penalties": 0,
            "confirmation_rejects": 0,
            "filters_rejects": 0,
            "selected": 0,
            "delivered": 0,
        }

        LOG.info("%s: starting modern analysis | trigger=%s", item.symbol, trigger)
        item = self._bot._refresh_universe_symbol_from_ws(item)

        minimums = self._minimums()
        rows_4h = frames.df_4h.height if frames.df_4h is not None else 0
        rows_5m = frames.df_5m.height if frames.df_5m is not None else 0
        rows_1h = frames.df_1h.height
        rows_15m = frames.df_15m.height
        funnel["frame_rows"] = {
            "15m": rows_15m,
            "1h": rows_1h,
            "5m": rows_5m,
            "4h": rows_4h,
        }
        funnel["frame_readiness"] = {
            "15m": rows_15m >= minimums["15m"],
            "1h": rows_1h >= minimums["1h"],
            "5m": rows_5m >= minimums["5m"],
            "4h": rows_4h >= minimums["4h"],
        }
        if rows_1h < minimums["1h"] or rows_15m < minimums["15m"]:
            missing_required = []
            if rows_15m < minimums["15m"]:
                missing_required.append("15m")
            if rows_1h < minimums["1h"]:
                missing_required.append("1h")
            rejected.append(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "symbol": item.symbol,
                    "setup_id": "data",
                    "direction": "n/a",
                    "stage": "data",
                    "reason": "insufficient_required_history",
                    "rows_1h": rows_1h,
                    "rows_15m": rows_15m,
                    "rows_5m": rows_5m,
                    "rows_4h": rows_4h,
                    "need_1h": minimums["1h"],
                    "need_15m": minimums["15m"],
                    "need_5m": minimums["5m"],
                    "need_4h": minimums["4h"],
                    "missing_required_frames": missing_required,
                }
            )
            LOG.warning(
                "%s: insufficient required history for analysis | 15m=%d/%d 1h=%d/%d optional_5m=%d/%d optional_4h=%d/%d",
                item.symbol,
                rows_15m,
                minimums["15m"],
                rows_1h,
                minimums["1h"],
                rows_5m,
                minimums["5m"],
                rows_4h,
                minimums["4h"],
            )
            _attach_rejection_rollups(funnel, rejected)
            return PipelineResult(
                symbol=item.symbol,
                trigger=trigger,
                event_ts=event_ts,
                raw_setups=0,
                candidates=candidates,
                rejected=rejected,
                status="insufficient_required_history",
                prepared=None,
                funnel=funnel,
            )

        try:
            # Build prepared symbol using modern prepare_symbol
            prepared = await asyncio.to_thread(
                prepare_symbol,
                item,
                frames,
                minimums=minimums,
                settings=self._bot.settings,
                ws_manager=self._bot._ws_manager,
            )
            LOG.debug(
                "%s: prepared symbol built | work_15m_rows=%s work_1h_rows=%s",
                item.symbol,
                prepared.work_15m.height
                if prepared is not None and prepared.work_15m is not None
                else 0,
                prepared.work_1h.height
                if prepared is not None and prepared.work_1h is not None
                else 0,
            )
        except Exception as exc:
            self._bot._prepare_error_count += 1
            error_payload = build_runtime_error_payload(
                component="symbol_analyzer.prepare_symbol",
                exc=exc,
                symbol=item.symbol,
                extra={"stage": "prepare_symbol", "ts": datetime.now(UTC).isoformat()},
            )
            self._bot._last_prepare_error = error_payload
            funnel["prepare_error_stage"] = "prepare_symbol"
            funnel["prepare_error_exception_type"] = type(exc).__name__
            funnel["prepare_error_class"] = error_payload["error_class"]
            LOG.warning("%s: failed to build prepared symbol: %s", item.symbol, exc)
            _attach_rejection_rollups(funnel, rejected)
            return PipelineResult(
                symbol=item.symbol,
                trigger=trigger,
                event_ts=event_ts,
                raw_setups=0,
                candidates=candidates,
                rejected=rejected,
                error=str(exc),
                status="prepare_error",
                prepared=prepared,
                funnel=funnel,
            )

        if prepared is not None and ws_enrichments:
            try:
                for key, value in ws_enrichments.items():
                    if hasattr(prepared, key):
                        setattr(prepared, key, value)
                # Debug: log enrichment status
                if ws_enrichments.get("mark_index_spread_bps") is not None:
                    LOG.debug(
                        "%s: enrichment mark_index_spread_bps=%.4f",
                        item.symbol,
                        ws_enrichments["mark_index_spread_bps"],
                    )
                else:
                    LOG.debug(
                        "%s: enrichment mark_index_spread_bps=None (ws_data_missing)",
                        item.symbol,
                    )
            except _DEGRADATION_ERRORS as exc:
                self._log_degradation(
                    level=logging.INFO,
                    symbol=item.symbol,
                    stage="ws_enrichment_apply",
                    source="ws_cache",
                    reason=str(exc),
                    fallback_used="skip_ws_enrichment",
                    exception_type=type(exc).__name__,
                )
            except Exception as exc:
                self._log_degradation(
                    level=logging.WARNING,
                    symbol=item.symbol,
                    stage="ws_enrichment_apply",
                    source="ws_cache",
                    reason=str(exc),
                    fallback_used="skip_ws_enrichment",
                    exception_type=type(exc).__name__,
                )

        if prepared is not None:
            try:
                market_ctx = await self._bot._modern_repo.get_market_context()
                for key in (
                    "btc_bias",
                    "eth_bias",
                    "altcoin_season_index",
                    "btc_phase",
                ):
                    value = market_ctx.get(key)
                    if value is not None and hasattr(prepared, key):
                        setattr(prepared, key, value)
            except _DEGRADATION_ERRORS as exc:
                self._log_degradation(
                    level=logging.INFO,
                    symbol=item.symbol,
                    stage="market_context",
                    source="memory",
                    reason=str(exc),
                    fallback_used="skip_multi_asset_context",
                    exception_type=type(exc).__name__,
                )
            except Exception as exc:
                self._log_degradation(
                    level=logging.WARNING,
                    symbol=item.symbol,
                    stage="market_context",
                    source="memory",
                    reason=str(exc),
                    fallback_used="skip_multi_asset_context",
                    exception_type=type(exc).__name__,
                )

        # Run modern engine (replaces pipeline analysis)
        if prepared is None:
            LOG.warning("%s: prepared symbol is None", item.symbol)
            _attach_rejection_rollups(funnel, rejected)
            return PipelineResult(
                symbol=item.symbol,
                trigger=trigger,
                event_ts=event_ts,
                raw_setups=0,
                candidates=candidates,
                rejected=rejected,
                status="prepare_failed",
                prepared=None,
                funnel=funnel,
            )

        # Log engine stats before calculation
        engine_stats = self._bot._modern_engine.get_engine_stats()
        LOG.debug(
            "%s: engine stats | enabled_strategies=%d total=%d",
            item.symbol,
            engine_stats.get("enabled_strategies", 0),
            engine_stats.get("total_strategies", 0),
        )
        self._bot._diagnostic_trace_counts[item.symbol] = 0

        try:
            signal_results = await self._bot._modern_engine.calculate_all(prepared)
            funnel["detector_runs"] = len(signal_results)
            LOG.debug(
                "%s: engine calculated | results_count=%d",
                item.symbol,
                len(signal_results),
            )
        except Exception as exc:
            error_class = classify_runtime_error(exc)
            funnel["engine_error_class"] = error_class
            LOG.warning(
                "%s: modern engine calculation failed: %s | error_class=%s",
                item.symbol,
                exc,
                error_class,
            )
            _attach_rejection_rollups(funnel, rejected)
            return PipelineResult(
                symbol=item.symbol,
                trigger=trigger,
                event_ts=event_ts,
                raw_setups=0,
                candidates=candidates,
                rejected=rejected,
                error=str(exc),
                status="engine_error",
                prepared=prepared,
                funnel=funnel,
            )

        # Process results: convert SignalResult to Signal, then apply the
        # production hard-gate + confluence path before a signal can become a
        # runtime candidate.
        signals_found = 0
        signals_rejected_perf = 0
        signals_added = 0

        for result in signal_results:
            setup_id = (
                result.setup_id
                or result.metadata.get("setup_id")
                or getattr(result.signal, "setup_id", "unknown")
            )
            decision = result.decision
            if decision is None:
                decision = StrategyDecision.error_result(
                    setup_id=setup_id,
                    reason_code="runtime.missing_decision",
                    error=result.error or "missing strategy decision",
                    stage="engine",
                    details={"symbol": item.symbol},
                )
            self._bot._append_strategy_decision_telemetry(
                symbol=item.symbol,
                trigger=trigger,
                decision=decision,
            )
            if decision.is_error or decision.is_skip or decision.is_reject:
                funnel["strategy_rejects_by_setup"][setup_id] = (
                    funnel["strategy_rejects_by_setup"].get(setup_id, 0) + 1
                )
                rejected.append(
                    self._bot._decision_to_reject_row(
                        symbol=item.symbol, decision=decision
                    )
                )
                LOG.debug(
                    "%s: strategy produced no signal | setup=%s status=%s reason=%s",
                    item.symbol,
                    setup_id,
                    decision.status,
                    decision.reason_code,
                )
                continue

            signal = decision.signal or result.signal
            if signal is None:
                fallback_decision = StrategyDecision.reject(
                    setup_id=setup_id,
                    stage="strategy",
                    reason_code="runtime.signal_missing_after_hit",
                    details={"symbol": item.symbol},
                )
                funnel["strategy_rejects_by_setup"][setup_id] = (
                    funnel["strategy_rejects_by_setup"].get(setup_id, 0) + 1
                )
                rejected.append(
                    self._bot._decision_to_reject_row(
                        symbol=item.symbol, decision=fallback_decision
                    )
                )
                continue

            setup_id = signal.setup_id
            metadata = self._bot._strategy_metadata(setup_id)
            signal = self._bot._apply_strategy_metadata(signal, metadata)

            precheck_ok, precheck_reason, precheck_details = self.check_family_precheck(
                signal,
                prepared,
                metadata,
            )
            if not precheck_ok:
                rejected.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": item.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "family_precheck",
                        "reason": precheck_reason or "family_precheck_reject",
                        "details": precheck_details,
                    }
                )
                funnel["family_precheck_rejects"] += 1
                continue
            if precheck_details.get("soft_penalty_applied"):
                penalty_factor = float(precheck_details.get("penalty_factor", 1.0))
                reason = str(
                    precheck_details.get("penalty_reason") or "family_precheck_penalty"
                )
                signal = replace(
                    signal,
                    score=round(max(signal.score * penalty_factor, 0.0), 4),
                    reasons=signal.reasons
                    if reason in signal.reasons
                    else (*signal.reasons, reason),
                )

            signal, alignment_details = self.apply_alignment_penalty(
                signal, prepared, metadata
            )
            if alignment_details.get("applied"):
                funnel["alignment_penalties"] += 1

            signals_found += 1
            funnel["raw_hits"] += 1
            funnel["raw_hits_by_setup"][signal.setup_id] = (
                funnel["raw_hits_by_setup"].get(signal.setup_id, 0) + 1
            )

            ltf_ok, ltf_reason, ltf_details = self.check_family_confirmation(
                signal, prepared, metadata
            )
            if not ltf_ok:
                rejected.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": item.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "confirmation",
                        "reason": ltf_reason or "5m_confirmation_reject",
                        "details": ltf_details,
                    }
                )
                funnel["confirmation_rejects"] += 1
                continue
            if ltf_details.get("fast_context_weak"):
                signal = replace(
                    signal,
                    score=round(max(signal.score * 0.95, 0.0), 4),
                    reasons=signal.reasons
                    if "fast_context_weak" in signal.reasons
                    else (*signal.reasons, "fast_context_weak"),
                )

            # Apply adaptive setup scoring using modern repo. A -0.05 penalty is
            # calibration input, not enough evidence to suppress every signal.
            score_adj = await self._bot._modern_repo.get_setup_score_adjustment(
                signal.setup_id
            )
            signal, perf_details = _apply_setup_score_adjustment(signal, score_adj)
            if perf_details.get("applied"):
                funnel["performance_adjustments"] = (
                    funnel.get("performance_adjustments", 0) + 1
                )
                self._bot._append_symbol_trace(
                    symbol=item.symbol,
                    row={
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": item.symbol,
                        "setup_id": signal.setup_id,
                        "stage": "performance_adjustment",
                        "details": perf_details,
                    },
                )

            passed, filtered_signal, filter_reason, scoring_result, filter_details = (
                apply_global_filters(
                    signal,
                    prepared,
                    self._bot.settings,
                    self._bot.confluence,
                )
            )
            if not passed:
                reject_row: dict[str, Any] = {
                    "ts": datetime.now(UTC).isoformat(),
                    "symbol": item.symbol,
                    "setup_id": signal.setup_id,
                    "direction": signal.direction,
                    "stage": "filters",
                    "reason": filter_reason or "filter_rejected",
                }
                if scoring_result is not None:
                    scoring_payload = scoring_result.to_dict()
                    scoring_payload["setup_id"] = signal.setup_id
                    reject_row["scoring"] = scoring_payload
                if filter_details:
                    reject_row["details"] = filter_details
                rejected.append(reject_row)
                funnel["filters_rejects"] += 1
                continue

            candidates.append(filtered_signal)
            signals_added += 1
            LOG.debug(
                "%s: candidate signal | setup=%s dir=%s score=%.3f rr=%.2f",
                item.symbol,
                filtered_signal.setup_id,
                filtered_signal.direction,
                filtered_signal.score,
                filtered_signal.risk_reward or 0,
            )

        LOG.info(
            "%s: analysis complete | trigger=%s raw_strategies=%d signals_found=%d perf_rejected=%d candidates=%d",
            item.symbol,
            trigger,
            len(signal_results),
            signals_found,
            signals_rejected_perf,
            signals_added,
        )
        funnel["post_filter_candidates"] = len(candidates)
        _attach_rejection_rollups(funnel, rejected)

        return PipelineResult(
            symbol=item.symbol,
            trigger=trigger,
            event_ts=event_ts,
            raw_setups=len(signal_results),
            candidates=candidates,
            rejected=rejected,
            status="no_setups" if len(signal_results) == 0 else "ok",
            prepared=prepared,
            funnel=funnel,
        )

    async def fetch_frames(self, item: UniverseSymbol) -> SymbolFrames | None:
        symbol = item.symbol
        minimums = self._minimums()
        limit_5m = _history_fetch_limit(minimums, "5m")
        limit_15m = _history_fetch_limit(minimums, "15m")
        limit_1h = _history_fetch_limit(minimums, "1h")
        limit_4h = _history_fetch_limit(minimums, "4h")

        ws_5m = ws_15m = ws_1h = None
        ws_bid = ws_ask = None
        if self._bot._ws_manager is not None:
            ws_frames = await self._bot._ws_manager.get_symbol_frames(symbol)
            if ws_frames is not None:
                ws_5m = ws_frames.df_5m
                ws_15m = ws_frames.df_15m
                ws_1h = ws_frames.df_1h
                ws_bid = ws_frames.bid_price
                ws_ask = ws_frames.ask_price

        try:
            if isinstance(self._bot.client, BinanceFuturesMarketData):
                df_4h = await self._bot.client.fetch_klines_cached(
                    symbol, "4h", limit=limit_4h
                )
                df_1h = (
                    ws_1h
                    if ws_1h is not None and ws_1h.height >= minimums["1h"]
                    else await self._bot.client.fetch_klines_cached(
                        symbol, "1h", limit=limit_1h
                    )
                )
                df_15m = (
                    ws_15m
                    if ws_15m is not None and ws_15m.height >= minimums["15m"]
                    else await self._bot.client.fetch_klines_cached(
                        symbol, "15m", limit=limit_15m
                    )
                )
                df_5m = (
                    ws_5m
                    if ws_5m is not None and ws_5m.height >= minimums["5m"]
                    else await self._bot.client.fetch_klines_cached(
                        symbol, "5m", limit=limit_5m
                    )
                )

                bid, ask = ws_bid, ws_ask
                if bid is None or ask is None:
                    bid, ask = await self._bot.client.fetch_book_ticker(symbol)

                return SymbolFrames(
                    symbol=symbol,
                    df_1h=df_1h,
                    df_15m=df_15m,
                    bid_price=bid,
                    ask_price=ask,
                    df_5m=df_5m,
                    df_4h=df_4h,
                )

            return await cast(Any, self._bot.client.fetch_symbol_frames(symbol))
        except MarketDataUnavailable as exc:
            LOG.warning("frame fetch failed for %s: %s", symbol, exc)
            return None
        except Exception:
            LOG.exception("unexpected frame fetch failure for %s", symbol)
            raise

    async def preload_shortlist_frames(self) -> None:
        await asyncio.sleep(1.0)
        if not isinstance(self._bot.client, BinanceFuturesMarketData):
            return
        minimums = self._minimums()
        async with self._bot._shortlist_lock:
            shortlist = list(self._bot._shortlist)
        if not shortlist:
            return

        batch_size = int(self._bot.settings.runtime.startup_batch_size)
        batch_delay = float(self._bot.settings.runtime.startup_batch_delay_seconds)
        sem = asyncio.Semaphore(
            int(self._bot.settings.runtime.max_concurrent_rest_requests)
        )

        async def _preload_one(symbol: str) -> None:
            async with sem:
                try:
                    await self._bot.client.fetch_klines_cached(
                        symbol, "5m", limit=_history_fetch_limit(minimums, "5m")
                    )
                    await self._bot.client.fetch_klines_cached(
                        symbol, "1h", limit=_history_fetch_limit(minimums, "1h")
                    )
                    await self._bot.client.fetch_klines_cached(
                        symbol, "15m", limit=_history_fetch_limit(minimums, "15m")
                    )
                    await self._bot.client.fetch_klines_cached(
                        symbol, "4h", limit=_history_fetch_limit(minimums, "4h")
                    )
                except (
                    MarketDataUnavailable,
                    RuntimeError,
                    ValueError,
                    TypeError,
                ) as exc:
                    self._log_degradation(
                        level=logging.WARNING,
                        symbol=symbol,
                        stage="shortlist_preload",
                        source="rest",
                        reason=str(exc),
                        fallback_used="skip_symbol_preload",
                        exception_type=type(exc).__name__,
                    )

        for i in range(0, len(shortlist), batch_size):
            batch = shortlist[i : i + batch_size]
            await asyncio.gather(
                *[_preload_one(item.symbol) for item in batch], return_exceptions=True
            )
            if i + batch_size < len(shortlist):
                await asyncio.sleep(batch_delay)

    def ws_cache_enrichments(self, symbol: str) -> dict[str, Any]:
        enrichments: dict[str, Any] = {}
        context_ages: list[float] = []
        freshness_flags: set[str] = set()
        degradation_events: list[dict[str, Any]] = []
        if self._bot._ws_manager is not None:
            try:
                ticker = self._bot._ws_manager.get_ticker_snapshot(symbol)
                ticker_age = self._bot._ws_manager.get_ticker_age_seconds(symbol)
                if ticker:
                    ticker_price = float(ticker.get("last_price") or 0.0)
                    if ticker_price > 0:
                        enrichments["ticker_price"] = ticker_price
                    if ticker_age is not None:
                        enrichments["ticker_price_age_seconds"] = ticker_age
                        context_ages.append(ticker_age)
                        if (
                            ticker_age
                            > self._bot.settings.ws.market_ticker_freshness_seconds
                        ):
                            freshness_flags.add("ticker_price_stale")
                else:
                    freshness_flags.add("ticker_price_missing")
            except _DEGRADATION_ERRORS as exc:
                degradation_events.append(
                    self._degrade_event(
                        symbol=symbol,
                        stage="ticker_snapshot",
                        source="ws",
                        reason=type(exc).__name__,
                        fallback_used="skip_ticker_enrichment",
                        exception_type=type(exc).__name__,
                    )
                )
                self._log_degradation(
                    level=logging.WARNING,
                    symbol=symbol,
                    stage="ticker_snapshot",
                    source="ws",
                    reason=str(exc),
                    fallback_used="skip_ticker_enrichment",
                    exception_type=type(exc).__name__,
                )
            try:
                mark = self._bot._ws_manager.get_mark_price_snapshot(symbol)
                mark_age = self._bot._ws_manager.get_mark_price_age_seconds(symbol)
                if mark:
                    mark_price = float(mark.get("mark_price") or 0.0)
                    if mark_price > 0:
                        enrichments["mark_price"] = mark_price
                    if "funding_rate" in mark:
                        enrichments["funding_rate"] = float(
                            mark.get("funding_rate") or 0.0
                        )
                    index_price = float(mark.get("index_price") or 0.0)
                    if mark_price > 0 and index_price > 0:
                        basis_pct = (mark_price - index_price) / index_price * 100.0
                        enrichments["basis_pct"] = basis_pct
                        enrichments["mark_index_spread_bps"] = basis_pct * 100.0
                if mark_age is not None:
                    enrichments["mark_price_age_seconds"] = mark_age
                    context_ages.append(mark_age)
                    if mark_age > self._bot.settings.ws.market_ticker_freshness_seconds:
                        freshness_flags.add("mark_price_stale")
                        degrade_reason = "mark_price_stale"
                        if "funding_rate" not in mark:
                            degrade_reason = "mark_price_stale_funding_missing"
                        degradation_events.append(
                            self._degrade_event(
                                symbol=symbol,
                                stage="mark_snapshot",
                                source="ws",
                                reason=degrade_reason,
                                fallback_used="rest_cached_funding",
                                exception_type="stale_cache",
                            )
                        )
                        self._log_degradation(
                            level=logging.INFO,
                            symbol=symbol,
                            stage="mark_snapshot",
                            source="ws",
                            reason=degrade_reason,
                            fallback_used="rest_cached_funding",
                            exception_type="stale_cache",
                        )
                elif not mark:
                    freshness_flags.add("mark_price_missing")
            except _DEGRADATION_ERRORS as exc:
                degradation_events.append(
                    self._degrade_event(
                        symbol=symbol,
                        stage="mark_snapshot",
                        source="ws",
                        reason=type(exc).__name__,
                        fallback_used="rest_cached_funding",
                        exception_type=type(exc).__name__,
                    )
                )
                self._log_degradation(
                    level=logging.WARNING,
                    symbol=symbol,
                    stage="mark_snapshot",
                    source="ws",
                    reason=str(exc),
                    fallback_used="rest_cached_funding",
                    exception_type=type(exc).__name__,
                )
            try:
                depth_imbalance = self._bot._ws_manager.get_depth_imbalance(symbol)
                if depth_imbalance is not None:
                    enrichments["depth_imbalance"] = float(depth_imbalance)
                microprice_bias = self._bot._ws_manager.get_microprice_bias(symbol)
                if microprice_bias is not None:
                    enrichments["microprice_bias"] = float(microprice_bias)
                liquidation = self._bot._ws_manager.get_liquidation_sentiment(
                    symbol=symbol, window_seconds=900
                )
                if liquidation is not None:
                    enrichments["liquidation_score"] = float(liquidation)
            except _DEGRADATION_ERRORS as exc:
                degradation_events.append(
                    self._degrade_event(
                        symbol=symbol,
                        stage="microstructure_snapshot",
                        source="ws",
                        reason=type(exc).__name__,
                        fallback_used="skip_microstructure_enrichment",
                        exception_type=type(exc).__name__,
                    )
                )
                self._log_degradation(
                    level=logging.WARNING,
                    symbol=symbol,
                    stage="microstructure_snapshot",
                    source="ws",
                    reason=str(exc),
                    fallback_used="skip_microstructure_enrichment",
                    exception_type=type(exc).__name__,
                )

        if isinstance(self._bot.client, BinanceFuturesMarketData):
            premium = self._bot.client.get_cached_premium_index(symbol)
            if premium is not None:
                mark_price = float(premium.get("mark_price") or 0.0)
                index_price = float(premium.get("index_price") or 0.0)
                if "funding_rate" not in enrichments:
                    enrichments["funding_rate"] = float(
                        premium.get("funding_rate") or 0.0
                    )
                if "mark_price" not in enrichments and mark_price > 0.0:
                    enrichments["mark_price"] = mark_price
                if (
                    "basis_pct" not in enrichments
                    and mark_price > 0.0
                    and index_price > 0.0
                ):
                    basis_pct = (mark_price - index_price) / index_price * 100.0
                    enrichments["basis_pct"] = basis_pct
                    enrichments.setdefault("mark_index_spread_bps", basis_pct * 100.0)
            elif "funding_rate" not in enrichments:
                funding_rate = self._bot.client.get_cached_funding_rate(symbol)
                if funding_rate is not None:
                    enrichments["funding_rate"] = funding_rate

            oi_chg = self._bot.client.get_cached_oi_change(symbol)
            if oi_chg is not None:
                enrichments["oi_change_pct"] = oi_chg
            else:
                degradation_events.append(
                    self._degrade_event(
                        symbol=symbol,
                        stage="oi_cache",
                        source="rest_cache",
                        reason="oi_change_missing",
                        fallback_used="skip_oi_context",
                        exception_type="stale_cache",
                    )
                )
                self._log_degradation(
                    level=logging.INFO,
                    symbol=symbol,
                    stage="oi_cache",
                    source="rest_cache",
                    reason="oi_change_missing",
                    fallback_used="skip_oi_context",
                    exception_type="stale_cache",
                )
            ls = self._bot.client.get_cached_ls_ratio(symbol)
            if ls is not None:
                enrichments["ls_ratio"] = ls
                enrichments["top_account_ls_ratio"] = ls
            else:
                degradation_events.append(
                    self._degrade_event(
                        symbol=symbol,
                        stage="ls_cache",
                        source="rest_cache",
                        reason="ls_ratio_missing",
                        fallback_used="skip_ls_context",
                        exception_type="stale_cache",
                    )
                )
                self._log_degradation(
                    level=logging.INFO,
                    symbol=symbol,
                    stage="ls_cache",
                    source="rest_cache",
                    reason="ls_ratio_missing",
                    fallback_used="skip_ls_context",
                    exception_type="stale_cache",
                )
            top_position_ls = self._bot.client.get_cached_top_position_ls_ratio(symbol)
            if top_position_ls is not None:
                enrichments["top_position_ls_ratio"] = top_position_ls
                enrichments["top_trader_position_ratio"] = top_position_ls
            taker = self._bot.client.get_cached_taker_ratio(symbol)
            if taker is not None:
                enrichments["taker_ratio"] = taker
            global_ls = self._bot.client.get_cached_global_ls_ratio(symbol)
            if global_ls is not None:
                enrichments["global_ls_ratio"] = global_ls
                enrichments["global_account_ls_ratio"] = global_ls
            funding_trend = self._bot.client.get_cached_funding_trend(symbol)
            if funding_trend is not None:
                enrichments["funding_trend"] = funding_trend
            else:
                degradation_events.append(
                    self._degrade_event(
                        symbol=symbol,
                        stage="funding_cache",
                        source="rest_cache",
                        reason="funding_trend_missing",
                        fallback_used="skip_funding_context",
                        exception_type="stale_cache",
                    )
                )
                self._log_degradation(
                    level=logging.INFO,
                    symbol=symbol,
                    stage="funding_cache",
                    source="rest_cache",
                    reason="funding_trend_missing",
                    fallback_used="skip_funding_context",
                    exception_type="stale_cache",
                )
            basis_pct = self._bot.client.get_cached_basis(symbol, period="1h")
            if basis_pct is not None:
                enrichments["basis_pct"] = basis_pct
            basis_stats = self._bot.client.get_cached_basis_stats(symbol, period="5m")
            if basis_stats is not None:
                premium_slope = basis_stats.get("premium_slope_5m")
                premium_zscore = basis_stats.get("premium_zscore_5m")
                mark_spread = basis_stats.get("mark_index_spread_bps")
                if premium_slope is not None:
                    enrichments["premium_slope_5m"] = float(premium_slope)
                if premium_zscore is not None:
                    enrichments["premium_zscore_5m"] = float(premium_zscore)
                if mark_spread is not None:
                    enrichments["mark_index_spread_bps"] = float(mark_spread)
            top = enrichments.get("ls_ratio")
            global_ls = enrichments.get("global_ls_ratio")
            if isinstance(top, (int, float)) and isinstance(global_ls, (int, float)):
                enrichments["top_vs_global_ls_gap"] = float(top) - float(global_ls)
            else:
                freshness_flags.add("crowding_context_missing")

        if context_ages:
            enrichments["context_snapshot_age_seconds"] = max(context_ages)
        if freshness_flags:
            enrichments["data_freshness_flags"] = tuple(sorted(freshness_flags))
        if degradation_events:
            primary = degradation_events[0]
            enrichments["degraded"] = True
            enrichments["degrade_reason"] = primary["degrade_reason"]
            enrichments["fallback_used"] = primary["fallback_used"]
            enrichments["degradation_events"] = tuple(degradation_events)
        enrichments.setdefault("data_source_mix", "futures_only")
        return enrichments

    def refresh_universe_symbol_from_ws(self, item: UniverseSymbol) -> UniverseSymbol:
        if self._bot._ws_manager is None:
            return item
        ticker = self._bot._ws_manager.get_ticker_snapshot(item.symbol)
        ticker_age = self._bot._ws_manager.get_ticker_age_seconds(item.symbol)
        if (
            not ticker
            or ticker_age is None
            or ticker_age > self._bot.settings.ws.market_ticker_freshness_seconds
        ):
            return item

        next_last_price = item.last_price
        try:
            ticker_last_price = float(ticker.get("last_price") or 0.0)
        except (TypeError, ValueError):
            return item
        if ticker_last_price > 0:
            next_last_price = ticker_last_price

        if next_last_price == item.last_price:
            return item
        return replace(item, last_price=next_last_price)

    async def ws_enrich(self, result: PipelineResult) -> None:
        if result.prepared is None:
            return
        p = result.prepared
        try:
            p.oi_current = await self._bot.client.fetch_open_interest(p.universe.symbol)
            p.oi_change_pct = await self._bot.client.fetch_open_interest_change(
                p.universe.symbol,
                period="1h",
            )
            p.oi_slope_5m = await self._bot.client.fetch_open_interest_change(
                p.universe.symbol,
                period="5m",
            )
        except _DEGRADATION_ERRORS as exc:
            self._log_degradation(
                level=logging.WARNING,
                symbol=p.universe.symbol,
                stage="oi_enrich_live",
                source="rest",
                reason=str(exc),
                fallback_used="cached_oi_context_only",
            )

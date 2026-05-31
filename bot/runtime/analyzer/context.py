from __future__ import annotations

from bot.runtime.analyzer.common import *  # noqa: F403


class AnalyzerContextMixin:
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
            min_bars_5m=self._bot.settings.filters.min_bars_5m,
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
            numeric if numeric == numeric and numeric not in (float("inf"), float("-inf")) else None
        )

    def _safe_ws_get(self, symbol: str, getter_name: str, *args: Any, **kwargs: Any) -> Any:
        manager = self._bot._ws_manager
        if manager is None:
            return None
        getter = getattr(manager, getter_name, None)
        if not callable(getter):
            return None
        try:
            return getter(symbol, *args, **kwargs)
        except self._DEGRADATION_ERRORS:
            return None

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
                value is not None for value in (top_account, top_position, global_ratio, gap)
            ),
            "exhaustion": exhaustion,
            "trend_support": trend_support,
            "headwind": headwind,
            "top_account_ls_ratio": top_account,
            "top_position_ls_ratio": top_position,
            "global_account_ls_ratio": global_ratio,
            "top_vs_global_ls_gap": gap,
        }

    def directional_context(self, signal: Signal, prepared: PreparedSymbol) -> dict[str, Any]:
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
                (prepared.premium_zscore_5m is not None and prepared.premium_zscore_5m <= -1.5)
                or (
                    prepared.mark_index_spread_bps is not None
                    and prepared.mark_index_spread_bps <= -8.0
                )
            )
            crowd_exhaustion = bool(crowding["exhaustion"])
            aggressor_reversal = bool(
                prepared.aggression_shift is not None and prepared.aggression_shift >= 0.03
            )
            regime_opposes = (
                prepared.regime_1h_confirmed == "downtrend" or prepared.bias_1h == "downtrend"
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
                (prepared.premium_zscore_5m is not None and prepared.premium_zscore_5m >= 1.5)
                or (
                    prepared.mark_index_spread_bps is not None
                    and prepared.mark_index_spread_bps >= 8.0
                )
            )
            crowd_exhaustion = bool(crowding["exhaustion"])
            aggressor_reversal = bool(
                prepared.aggression_shift is not None and prepared.aggression_shift <= -0.03
            )
            regime_opposes = (
                prepared.regime_1h_confirmed == "uptrend" or prepared.bias_1h == "uptrend"
            )
            flow_opposes = bool(flow_proxy is not None and flow_proxy >= 0.03)
        exhaustion_hits = {
            "premium_extreme": premium_exhaustion,
            "liquidation_imbalance": bool(
                prepared.liquidation_score is not None and prepared.liquidation_score <= -0.35
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
        adx_1h = self._frame_float(prepared.work_1h, "adx14")
        adx_15m = self._frame_float(prepared.work_15m, "adx14")
        regime_adx = adx_1h if adx_1h is not None else adx_15m
        details["adx_1h"] = adx_1h
        details["adx_15m"] = adx_15m
        trend_regime_setups = {
            "bos_choch",
            "structure_pullback",
            "ema_bounce",
            "supertrend_follow",
            "keltner_breakout",
            "multi_tf_trend",
            "hidden_divergence",
        }
        range_regime_setups = {
            "absorption",
            "bb_squeeze",
            "liquidity_sweep",
            "squeeze_setup",
            "stop_hunt_detection",
            "turtle_soup",
            "volume_climax_reversal",
            "wick_trap_reversal",
            "wyckoff_spring",
        }
        if regime_adx is not None:
            if signal.setup_id in trend_regime_setups and regime_adx < 20.0:
                details["regime_filter"] = "trend_required_adx_lt_20"
                details["soft_penalty_applied"] = True
                details["penalty_factor"] = 0.90
                details["penalty_reason"] = "context.low_adx_trend_setup_penalty"
                return True, None, details
            if signal.setup_id in range_regime_setups and regime_adx > 40.0:
                details["soft_penalty_applied"] = True
                details["penalty_factor"] = 0.88
                details["penalty_reason"] = "context.range_setup_in_strong_trend"
                return True, None, details
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
        if profile == "trend_follow" and details["flow_opposes"] and not details["trend_confirms"]:
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
        if opposing_votes == 0 or family == "reversal" or profile == "countertrend_exhaustion":
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
            details["confirmation_votes"]["crowding_support"] = details["crowd_trend_support"]
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
                details["relaxed_reject"] = f"breakout_crowding_unconfirmed_{signal.direction}"
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


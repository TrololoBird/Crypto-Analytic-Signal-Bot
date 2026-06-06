"""Analyzer family gate mixins (extracted from symbol_analyzer.py)."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from bot.runtime_policy import is_deep_analysis_symbol

if TYPE_CHECKING:
    from bot.domain.schemas import PreparedSymbol, Signal
    from bot.runtime.bot import SignalBot


class AnalyzerMixinBase:
    """Declares ``_bot`` for all analyzer mixins."""

    _bot: SignalBot


class AnalyzerFamilyGatesMixin(AnalyzerMixinBase):
    @staticmethod
    def _htf_direction_allows(
        signal: Signal,
        prepared: PreparedSymbol,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        """Symmetric HTF bias gate: longs need 4h uptrend; shorts need 4h downtrend."""
        bias_1h = str(getattr(prepared, "bias_1h", "") or "neutral").lower()
        bias_4h = str(getattr(prepared, "bias_4h", "") or "neutral").lower()
        details: dict[str, Any] = {
            "bias_1h": bias_1h,
            "bias_4h": bias_4h,
            "signal_direction": signal.direction,
        }
        direction = str(signal.direction or "").lower()
        if direction == "long":
            if bias_4h == "uptrend" and bias_1h in {"uptrend", "neutral"}:
                details["htf_gate"] = "long_allowed_4h_uptrend"
                return True, None, details
            if bias_1h == "downtrend" and bias_4h != "uptrend":
                return False, "long_blocked_without_4h_uptrend", details
        elif direction == "short":
            if bias_4h == "downtrend" and bias_1h in {"downtrend", "neutral"}:
                details["htf_gate"] = "short_allowed_4h_downtrend"
                return True, None, details
            if bias_1h == "uptrend" and bias_4h != "downtrend":
                return False, "short_blocked_without_4h_downtrend", details
        if bias_1h == "neutral" and bias_4h == "neutral":
            details["htf_gate"] = "neutral_htf_soft_pass"
        return True, None, details

    def check_family_precheck(
        self,
        signal: Signal,
        prepared: PreparedSymbol,
        metadata: Any | None,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        details = self.directional_context(signal, prepared)
        family = getattr(metadata, "family", signal.strategy_family)
        profile = getattr(metadata, "confirmation_profile", signal.confirmation_profile)
        htf_ok, htf_reason, htf_details = self._htf_direction_allows(signal, prepared)
        details.update(htf_details)
        if not htf_ok:
            return False, htf_reason, details
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
            bias_4h = str(getattr(prepared, "bias_4h", "") or "neutral").lower()
            if signal.direction == "long" and bias_4h == "uptrend":
                details["relaxed_reject"] = f"flow_precheck_opposes_{signal.direction}"
                return True, None, details
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
            if signal.direction == "long" and details["confirmation_count"] >= 1:
                details["relaxed_reject"] = f"hard_context_opposes_{signal.direction}"
                return True, None, details
            return False, f"hard_context_opposes_{signal.direction}", details
        if deep_analysis_asset and (
            primary_timeframe in {"1h", "4h"} or details["confirmation_count"] >= 1
        ):
            details["relaxed_reject"] = f"5m_opposes_{signal.direction}"
            return True, None, details
        return False, f"5m_opposes_{signal.direction}", details

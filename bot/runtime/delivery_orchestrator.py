"""Delivery orchestration: merge → contract → confluence → tiers → journal → Telegram.

Persistence ordering (crash-safe):
1. ``SignalTracker.arm_signals`` writes the pending journal row (SQLite + tracking_events).
2. ``SignalDelivery.deliver`` sends Telegram (and public audit CSV on success).
3. ``SignalTracker.update_signal_message_ids`` links the Telegram message id.
4. On non-sent delivery, ``cancel_pending_delivery`` closes the pending journal row.

Telemetry ``selected.jsonl`` is appended by ``cycle_runner`` after this orchestrator returns.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter, deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.delivery import contract as _delivery_contract_module
from bot.delivery.confluence import ConfluenceEngine, evaluate_weighted_delivery_gate
from bot.delivery.ops_webhook import notify_ops_delivery_failed, notify_ops_tier_cap_starvation
from bot.delivery.telegram_routing import (
    operator_dm_enabled,
    send_operator_analytics_companion,
    should_send_channel_analytics_companion,
)
from bot.delivery.tiers import _finite_score, decide_with_caps
from bot.delivery.tiers import rank_key as tier_rank_key
from bot.domain.delivery_policy import (
    is_positioning_setup,
    r_class_blocks_action,
    resolve_bear_regime,
)
from bot.domain.mtf import (
    BREAKOUT_PROFILE,
    REVERSAL_PROFILES,
    evaluate_mtf_gate,
    normalize_mtf_reject_reason,
)
from bot.persistence.outcomes import build_prepared_feature_snapshot, extract_features_from_signal
from bot.runtime.errors import DEFENSIVE_EXC

from .merge import MetaSignalMerger
from .sl_postmortem import build_sl_postmortem_html
from .telegram_operator import TelegramOperatorConsole, operator_console_enabled
from .watch_escalation import maybe_notify_watch_escalation

if TYPE_CHECKING:
    from bot.delivery.contract import SignalContractIssue
    from bot.domain.schemas import PreparedSymbol, Signal
    from bot.persistence.tracking import SignalTrackingEvent
    from bot.runtime.bot import SignalBot

    class _DeliveryOrchestratorBases:
        _bot: SignalBot

        @staticmethod
        def _contract_issue_rows(signal: Signal) -> list[dict[str, object]]: ...

        def _new_portfolio_cap_state(self) -> dict[str, Any]: ...

        def _limit_entry_gate(
            self,
            signal: Signal,
            prepared: PreparedSymbol | None,
        ) -> tuple[bool, str | None, dict[str, object]]: ...

        def _queue_ready_signal(
            self,
            signal: Signal,
            *,
            portfolio_state: dict[str, Any],
            ready_to_send: list[Signal],
            queued_setup_ids: set[str],
            queued_symbol_direction: set[str],
            rejected_rows: list[dict[str, Any]],
            symbol_direction_key: str | None = None,
        ) -> bool: ...

        @staticmethod
        def _symbol_direction_cooldown_key(signal: Signal) -> str: ...

        def _record_watch_screener(self, *args: Any, **kwargs: Any) -> None: ...

        def _record_delivery_attempt(self, *args: Any, **kwargs: Any) -> None: ...
else:
    from bot.runtime._delivery_ranking import DeliveryRankingMixin
    from bot.runtime._delivery_watch import DeliveryWatchMixin

    class _DeliveryOrchestratorBases(DeliveryRankingMixin, DeliveryWatchMixin):
        pass


LOG = logging.getLogger("bot.runtime.bot")
_DEFAULT_MIN_CONFIRMATIONS = 3  # confirmations: ADR-003 hard confluence gate
WEIGHTED_HARD_LEG_KEYS = ("trend", "momentum", "volume")
MIN_WEIGHTED_HARD_LEGS = 2
DELIVERY_SUCCESS_STATUSES = frozenset({"sent", "logged"})


def _delivery_contract_gate_order_anchor(signal: Signal) -> list[SignalContractIssue]:
    """Static audit anchor; runtime path uses ``_contract_issue_rows`` in select_and_deliver."""
    return _delivery_contract_module.validate_signal_contract(signal)


class DeliveryOrchestrator(_DeliveryOrchestratorBases):
    def __init__(self, bot: SignalBot) -> None:
        self._bot = bot
        self._delivery_burst_times: deque[float] = deque(maxlen=256)

    def _burst_delivery_allows(self) -> tuple[bool, int]:
        cap = int(getattr(self._bot.settings.delivery, "max_signals_per_minute", 0) or 0)
        if cap <= 0:
            return True, 0
        now = time.monotonic()
        window_start = now - 60.0
        while self._delivery_burst_times and self._delivery_burst_times[0] < window_start:
            self._delivery_burst_times.popleft()
        if len(self._delivery_burst_times) >= cap:
            return False, cap
        return True, cap

    def _record_burst_delivery(self) -> None:
        self._delivery_burst_times.append(time.monotonic())

    def _record_delivery_diag_reject(
        self,
        stage: str,
        reason: str,
        *,
        setup_id: str,
    ) -> None:
        diagnostics = getattr(self._bot, "_signal_diagnostics", None)
        if diagnostics is None:
            return
        diagnostics.record_delivery_stage_reject(stage, reason, setup_id=setup_id)

    def _record_delivery_diag_delivered(self, setup_id: str) -> None:
        diagnostics = getattr(self._bot, "_signal_diagnostics", None)
        if diagnostics is None:
            return
        diagnostics.record_delivered(setup_id)

    def _record_metrics_delivered(self, signal: Signal) -> None:
        metrics = getattr(self._bot, "metrics", None)
        if metrics is None:
            return
        record = getattr(metrics, "record_signal_delivered", None)
        if callable(record):
            record(signal.setup_id, signal.direction)

    def _record_metrics_rejected(self, signal: Signal, reason: str) -> None:
        metrics = getattr(self._bot, "metrics", None)
        if metrics is None:
            return
        record = getattr(metrics, "record_signal_rejected", None)
        if callable(record):
            record(signal.setup_id, signal.direction, reason)

    @staticmethod
    def _latest_float(frame: Any, column: str) -> float | None:
        if frame is None or frame.is_empty() or column not in frame.columns:
            return None
        try:
            raw = frame.item(-1, column)
            value = float(raw) if raw is not None else None
        except (IndexError, TypeError, ValueError):
            return None
        return value if value is not None and math.isfinite(value) else None

    @staticmethod
    def _tail_mean(frame: Any, column: str, window: int) -> float | None:
        if frame is None or frame.is_empty() or column not in frame.columns:
            return None
        values: list[float] = []
        for raw in frame[column].tail(max(1, int(window))).to_list():
            try:
                value = float(raw) if raw is not None else math.nan
            except (TypeError, ValueError):
                value = math.nan
            if math.isfinite(value):
                values.append(value)
        if not values:
            return None
        return sum(values) / len(values)

    @staticmethod
    def _direction(signal: Signal) -> str:
        return str(signal.direction or "").strip().lower()

    @classmethod
    def _trend_confirmation(
        cls,
        *,
        direction: str,
        profile: str,
        close: float | None,
        ema20: float | None,
        ema50: float | None,
    ) -> bool:
        if close is None or ema20 is None:
            return False
        if profile in REVERSAL_PROFILES:
            if direction == "long":
                return close < ema20 or (ema50 is not None and close < ema20 < ema50)
            if direction == "short":
                return close > ema20 or (ema50 is not None and close > ema20 > ema50)
            return False
        if profile == BREAKOUT_PROFILE:
            if direction == "long":
                return close > ema20
            if direction == "short":
                return close < ema20
            return False
        if ema50 is None:
            return False
        if direction == "long":
            return bool(close > ema20 > ema50)
        if direction == "short":
            return bool(close < ema20 < ema50)
        return False

    @classmethod
    def _momentum_confirmation(
        cls,
        *,
        direction: str,
        profile: str,
        rsi: float | None,
    ) -> bool:
        if rsi is None:
            return False
        if profile in REVERSAL_PROFILES:
            if direction == "long":
                return rsi < 50.0
            if direction == "short":
                return rsi > 50.0
            return False
        if profile == BREAKOUT_PROFILE:
            if direction == "long":
                return 40.0 < rsi < 75.0
            if direction == "short":
                return 25.0 < rsi < 60.0
            return False
        if direction == "long":
            return 30.0 < rsi < 65.0
        if direction == "short":
            return 35.0 < rsi < 70.0
        return False

    @classmethod
    def _volume_confirmation(
        cls,
        *,
        profile: str,
        volume: float | None,
        volume_avg: float | None,
    ) -> bool:
        if volume is None or volume_avg is None or volume_avg <= 0.0:
            return False
        if profile == BREAKOUT_PROFILE:
            multiplier = 1.1
        elif profile in REVERSAL_PROFILES:
            multiplier = 1.0
        else:
            multiplier = 1.2
        return bool(volume > volume_avg * multiplier)

    @classmethod
    def _microstructure_confirmation(
        cls,
        *,
        direction: str,
        prepared: PreparedSymbol,
        setup_id: str = "",
    ) -> tuple[bool, dict[str, object]]:
        details: dict[str, object] = {}
        micro = getattr(prepared, "microprice_bias", None)
        agg = getattr(prepared, "agg_trade_delta_30s", None)

        micro_ok = False
        if micro is not None:
            try:
                micro_val = float(micro)
            except (TypeError, ValueError):
                micro_val = math.nan
            if math.isfinite(micro_val):
                details["microprice_bias"] = micro_val
                if direction == "long":
                    micro_ok = micro_val >= 0.05
                elif direction == "short":
                    micro_ok = micro_val <= -0.05

        agg_ok = False
        if agg is not None:
            try:
                agg_val = float(agg)
            except (TypeError, ValueError):
                agg_val = math.nan
            if math.isfinite(agg_val):
                details["agg_trade_delta_30s"] = agg_val
                if direction == "long":
                    agg_ok = agg_val >= 0.0
                elif direction == "short":
                    agg_ok = agg_val <= 0.0

        if micro_ok or agg_ok:
            details["microstructure_source"] = "live_micro"
            return True, details

        funding = getattr(prepared, "funding_rate", None)
        oi_change = getattr(prepared, "oi_change_pct", None)
        try:
            funding_value = float(funding) if funding is not None else 0.0
        except (TypeError, ValueError):
            funding_value = 0.0
        try:
            oi_value = float(oi_change) if oi_change is not None else 0.0
        except (TypeError, ValueError):
            oi_value = 0.0
        if not math.isfinite(funding_value):
            funding_value = 0.0
        if not math.isfinite(oi_value):
            oi_value = 0.0
        details["funding_rate"] = funding_value
        details["oi_change_pct"] = oi_value
        details["microstructure_source"] = "funding_oi_proxy"
        if is_positioning_setup(setup_id):
            # Positioning setups need elevated funding/OI - not a calm-market proxy.
            funding_extreme = abs(funding_value) >= 0.0003
            oi_extreme = abs(oi_value) >= 0.5
            return funding_extreme or oi_extreme, details
        return abs(funding_value) < 0.001 and abs(oi_value) < 12.0, details

    def _confluence_gate_options(self) -> dict[str, bool | int]:
        delivery = self._bot.settings.delivery
        return {
            "enforce_mtf_gate": bool(getattr(delivery, "enforce_mtf_gate", True)),
            "min_confirmations": int(
                getattr(delivery, "min_confirmations", _DEFAULT_MIN_CONFIRMATIONS)
            ),
            "reversal_min_confirmations": int(getattr(delivery, "reversal_min_confirmations", 3)),
            "use_weighted_confluence": bool(getattr(delivery, "use_weighted_confluence", True)),
            "weighted_min_hard_legs": int(getattr(delivery, "weighted_min_hard_legs", 2)),
        }

    def _confluence_gate_kwargs(self) -> dict[str, Any]:
        opts: dict[str, Any] = dict(self._confluence_gate_options())
        opts["settings"] = self._bot.settings
        opts["confluence_engine"] = getattr(self._bot, "confluence", None)
        return opts

    @classmethod
    def _hard_confluence_gate(
        cls,
        signal: Signal,
        prepared: PreparedSymbol | None,
        *,
        enforce_mtf_gate: bool = True,
        min_confirmations: int = _DEFAULT_MIN_CONFIRMATIONS,
        reversal_min_confirmations: int = 3,
        use_weighted_confluence: bool = True,
        weighted_min_hard_legs: int = MIN_WEIGHTED_HARD_LEGS,
        settings: Any | None = None,
        confluence_engine: Any | None = None,
    ) -> tuple[bool, dict[str, bool], dict[str, object]]:
        """Require at least 3 independent confirmations before delivery."""
        empty_confirmations = {
            "trend": False,
            "momentum": False,
            "volume": False,
            "htf": False,
            "microstructure": False,
        }
        if prepared is None:
            return (
                False,
                empty_confirmations,
                {
                    "reason": "prepared_context_missing",
                    "bear_regime": False,
                    "bear_regime_source": "none",
                    "btc_phase": "unknown",
                    "btc_phase_rule": "none",
                    "required": min_confirmations,
                    "confirmed": 0,
                    "mtf_reason": "",
                },
            )

        primary = getattr(prepared, "work_primary", None)
        if primary is None:
            primary = prepared.work_15m
        direction = cls._direction(signal)
        close = cls._latest_float(primary, "close")
        ema20 = cls._latest_float(primary, "ema20")
        ema50 = cls._latest_float(primary, "ema50")
        rsi = cls._latest_float(primary, "rsi14")
        volume = cls._latest_float(primary, "volume")
        volume_avg = cls._tail_mean(primary, "volume", 20)

        profile = str(getattr(signal, "confirmation_profile", "trend_follow") or "trend_follow")
        trend = cls._trend_confirmation(
            direction=direction,
            profile=profile,
            close=close,
            ema20=ema20,
            ema50=ema50,
        )
        momentum = cls._momentum_confirmation(direction=direction, profile=profile, rsi=rsi)
        volume_ok = cls._volume_confirmation(
            profile=profile,
            volume=volume,
            volume_avg=volume_avg,
        )

        prepared_settings = getattr(prepared, "settings", None)
        strict_data_quality = bool(
            getattr(getattr(prepared_settings, "runtime", None), "strict_data_quality", True)
        )
        mtf_ok, mtf_reason, mtf_details = evaluate_mtf_gate(
            prepared,
            direction,
            confirmation_profile=profile,
            strict_data_quality=strict_data_quality,
        )

        regime_1h = str(
            getattr(prepared, "regime_1h_confirmed", None)
            or getattr(prepared, "bias_1h", None)
            or "neutral"
        ).lower()
        regime_4h = str(
            getattr(prepared, "regime_4h_confirmed", None)
            or getattr(prepared, "bias_4h", None)
            or "neutral"
        ).lower()

        market_ctx = getattr(prepared, "market_ctx", None)
        bear_regime, bear_regime_source = resolve_bear_regime(
            market_ctx=market_ctx if isinstance(market_ctx, dict) else None,
            prepared_btc_bias=getattr(prepared, "btc_bias", None),
            signal_btc_bias=getattr(signal, "btc_bias", None),
        )

        btc_phase = str(getattr(prepared, "btc_phase", "") or "").strip().lower()
        if not btc_phase and isinstance(market_ctx, dict):
            btc_phase = str(market_ctx.get("btc_phase") or "").strip().lower()
        if (
            profile in REVERSAL_PROFILES
            and direction == "long"
            and btc_phase
            in {
                "decline",
                "distribution",
            }
        ):
            btc_phase_rule = "countertrend_decline_penalty_eligible"
        elif (
            profile in REVERSAL_PROFILES
            and direction == "short"
            and btc_phase
            in {
                "markup",
                "accumulation",
            }
        ):
            btc_phase_rule = "countertrend_markup_penalty_eligible"
        else:
            btc_phase_rule = "none"

        htf_conflict = str(mtf_reason or "").startswith("htf_reversal_conflict")
        if profile in REVERSAL_PROFILES and bear_regime and direction == "long" and htf_conflict:
            details_pre: dict[str, object] = {
                "htf_reversal_conflict_bear": True,
                "reason": "htf_reversal_conflict_bear",
                "bear_regime": bear_regime,
                "btc_phase": btc_phase or "unknown",
            }
            return False, empty_confirmations, details_pre

        htf = mtf_ok if enforce_mtf_gate else True
        microstructure, micro_details = cls._microstructure_confirmation(
            direction=direction,
            prepared=prepared,
            setup_id=str(getattr(signal, "setup_id", "") or ""),
        )

        confirmations = {
            "trend": trend,
            "momentum": momentum,
            "volume": volume_ok,
            "htf": htf,
            "microstructure": microstructure,
        }
        confirmation_count = sum(confirmations.values())
        required = min_confirmations
        if profile in REVERSAL_PROFILES and bear_regime:
            required = max(required, min(int(reversal_min_confirmations), 5))
        if btc_phase_rule != "none" and profile in REVERSAL_PROFILES:
            required = max(required, min_confirmations + 1)
        details: dict[str, object] = {
            "confirmed": confirmation_count,
            "required": required,
            "bear_regime": bear_regime,
            "bear_regime_source": bear_regime_source,
            "btc_phase": btc_phase or "unknown",
            "btc_phase_rule": btc_phase_rule,
            "close": close,
            "ema20": ema20,
            "ema50": ema50,
            "rsi14": rsi,
            "volume": volume,
            "volume_mean20": volume_avg,
            "regime_1h": regime_1h,
            "regime_4h": regime_4h,
            "mtf_reason": mtf_reason,
            "mtf_details": mtf_details,
            "mtf_enforced": enforce_mtf_gate,
            "confirmation_profile": profile,
            **micro_details,
        }
        boolean_pass = confirmation_count >= required
        if use_weighted_confluence and settings is not None and prepared is not None:
            engine = confluence_engine or ConfluenceEngine(settings)
            conf_result = engine.score(signal, prepared)
            delivery_cfg = getattr(settings, "delivery", None)
            min_hard = int(
                getattr(delivery_cfg, "weighted_min_hard_legs", weighted_min_hard_legs)
                or weighted_min_hard_legs
            )
            if profile in REVERSAL_PROFILES and bear_regime:
                min_hard = max(
                    min_hard,
                    min(int(reversal_min_confirmations), len(WEIGHTED_HARD_LEG_KEYS)),
                )
            if btc_phase_rule != "none" and profile in REVERSAL_PROFILES:
                min_hard = max(min_hard, min(required, len(WEIGHTED_HARD_LEG_KEYS)))
            boolean_pass, weighted_details = evaluate_weighted_delivery_gate(
                conf_result=conf_result,
                confirmations=confirmations,
                action_min_score=float(settings.delivery.action_min_score),
                min_hard_legs=min_hard,
                hard_leg_keys=WEIGHTED_HARD_LEG_KEYS,
            )
            details.update(weighted_details)
            details["boolean_confirmations"] = confirmation_count
            details["boolean_required"] = required
        elif use_weighted_confluence:
            details["weighted_confluence_primary"] = True
        if enforce_mtf_gate and not mtf_ok:
            details["reason"] = normalize_mtf_reject_reason(mtf_reason)
            return False, confirmations, details
        return boolean_pass, confirmations, details

    async def _send_sl_postmortem_to_operators(self, events: list[SignalTrackingEvent]) -> None:
        if not bool(getattr(self._bot.settings.delivery, "sl_postmortem_enabled", True)):
            return
        if not operator_console_enabled(self._bot):
            return
        if not operator_dm_enabled(self._bot, "send_sl_postmortem"):
            return
        console = getattr(self._bot, "_operator_console", None)
        if console is None:
            console = TelegramOperatorConsole(self._bot)
        for event in events:
            try:
                await console.send_html_to_operators(build_sl_postmortem_html(event))
            except DEFENSIVE_EXC:
                LOG.debug("sl postmortem operator notify skipped", exc_info=True)

    async def close_superseded_signal(self, new_signal: Signal) -> list[SignalTrackingEvent] | None:
        try:
            superseded: (
                list[SignalTrackingEvent] | None
            ) = await self._bot.tracker.supersede_open_signal(new_signal, dry_run=False)
        except DEFENSIVE_EXC as exc:
            LOG.debug("supersede failed for %s: %s", new_signal.symbol, exc)
            return None
        else:
            return superseded

    def _quality_monitor_rejects(
        self,
        signal: Signal,
        rejected_rows: list[dict[str, Any]],
    ) -> bool:
        monitor = getattr(self._bot, "quality_monitor", None)
        if monitor is None:
            return False
        health = monitor.get_setup_health(signal.setup_id)
        if health.get("recommendation") == "pause" and int(health.get("sample_count", 0)) < 30:
            health["recommendation"] = "keep"
            health.setdefault("reasons", [])
            health["reasons"] = [*health["reasons"], "pause_override_insufficient_samples"]
        throttle = bool(
            health.get("recommendation") == "pause"
            or monitor.should_throttle_delivery(signal.setup_id, signal.symbol)
        )
        if not throttle:
            return False
        rejected_rows.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "symbol": signal.symbol,
                "setup_id": signal.setup_id,
                "direction": signal.direction,
                "stage": "quality_monitor",
                "reason": "quality_monitor_pause",
                "quality_health": health,
            }
        )
        LOG.warning(
            (
                "quality monitor paused delivery | symbol=%s setup=%s recommendation=%s "
                "consecutive_losses=%s win_rate=%s"
            ),
            signal.symbol,
            signal.setup_id,
            health.get("recommendation"),
            health.get("consecutive_losses"),
            health.get("win_rate"),
        )
        return True

    async def _deliver_direction_conflict_watch(
        self,
        conflict_signals: list[Signal],
        *,
        prepared_by_tracking_id: dict[str, PreparedSymbol] | None,
        rejected_rows: list[dict[str, Any]],
        delivery_status_counts: Counter[str],
        btc_bias: str,
        eth_bias: str,
    ) -> list[Signal]:
        """Send merge losers as WATCH conflict (P1 SIGNAL_COLLISION spec)."""
        delivered: list[Signal] = []
        for signal in conflict_signals:
            if self._contract_issue_rows(signal):
                continue
            prepared = (
                prepared_by_tracking_id.get(signal.tracking_id) if prepared_by_tracking_id else None
            )
            gate_opts = self._confluence_gate_kwargs()
            gate_passed, confirmations, gate_details = self._hard_confluence_gate(
                signal,
                prepared,
                **gate_opts,
            )
            if not gate_passed:
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "confluence",
                        "reason": "direction_conflict_watch_gate_failed",
                        "confirmations": confirmations,
                        "details": gate_details,
                    }
                )
                continue
            tier_map = {signal.tracking_id: "watch"}
            armed_ok, _ = await self._bot._wait_noncritical(
                label=f"journal conflict {signal.symbol}/{signal.setup_id}",
                max_wait_s=self._bot._noncritical_timeout_seconds,
                operation=self._bot.tracker.arm_signals([signal], dry_run=False),
            )
            if not armed_ok:
                continue
            ok, results = await self._bot._wait_noncritical(
                label=f"deliver conflict {signal.symbol}/{signal.setup_id}",
                max_wait_s=self._bot._delivery_timeout_seconds,
                operation=self._bot.delivery.deliver(
                    [signal],
                    dry_run=False,
                    btc_bias=btc_bias,
                    eth_bias=eth_bias,
                    tier_by_tracking_id=tier_map,
                ),
            )
            if not ok or not results:
                await self._bot.tracker.cancel_pending_delivery(signal)
                continue
            for item in results:
                delivery_status_counts[item.status] += 1
                if item.status not in DELIVERY_SUCCESS_STATUSES:
                    await self._bot.tracker.cancel_pending_delivery(item.signal)
                    continue
                delivered.append(item.signal)
                self._record_delivery_diag_delivered(item.signal.setup_id)
                self._record_metrics_delivered(item.signal)
                dashboard = getattr(self._bot, "dashboard", None)
                if dashboard is not None:
                    dashboard.notify_signal_delivered(item.signal)
                if item.message_id is not None:
                    await self._bot._wait_noncritical(
                        label=f"link conflict {item.signal.symbol}/{item.signal.setup_id}",
                        max_wait_s=self._bot._noncritical_timeout_seconds,
                        operation=self._bot.tracker.update_signal_message_ids(
                            {item.signal.tracking_id: item.message_id},
                            dry_run=False,
                        ),
                    )
        return delivered

    async def select_and_deliver(
        self,
        signals: list[Signal],
        *,
        prepared_by_tracking_id: dict[str, PreparedSymbol] | None = None,
    ) -> tuple[list[Signal], list[dict[str, Any]], Counter[str], int]:
        if not signals:
            return [], [], Counter(), 0

        ledger = getattr(self._bot, "public_audit", None)
        recent_actions: list[Signal] = []
        action_window_hours = float(self._bot.settings.tracking.action_window_hours)
        if ledger is not None and hasattr(ledger, "recent_action_signals"):
            recent_actions = ledger.recent_action_signals(within_hours=action_window_hours)

        merge_result = MetaSignalMerger(action_window_hours=action_window_hours).merge(
            signals,
            recent_actions=recent_actions,
        )
        merged_meta = merge_result.merged
        direction_conflict_signals = [meta.primary for meta in merge_result.direction_conflicts]
        merge_conflict_count = len(merge_result.direction_conflicts)
        merge_meta_by_tracking_id = {meta.primary.tracking_id: meta for meta in merged_meta}
        signals = [meta.primary for meta in merged_meta]

        ready_to_send: list[Signal] = []
        rejected_rows: list[dict[str, Any]] = []
        queued_symbol_direction: set[str] = set()
        queued_family_keys: set[str] = set()
        queued_setup_ids: set[str] = set()
        contract_validated_tracking_ids: set[str] = set()
        confluence_passed_tracking_ids: set[str] = set()
        tier_allowed_tracking_ids: set[str] = set()
        tier_ranked_signals = sorted(signals, key=tier_rank_key, reverse=True)
        tier_decisions = decide_with_caps(tier_ranked_signals, self._bot.settings)
        tier_by_tracking_id = dict(
            zip(
                [signal.tracking_id for signal in tier_ranked_signals],
                tier_decisions,
                strict=True,
            )
        )
        portfolio_state = self._new_portfolio_cap_state()

        for signal in signals:
            contract_issues = self._contract_issue_rows(signal)
            if contract_issues:
                self._record_delivery_diag_reject(
                    "contract", "invalid_signal_contract", setup_id=signal.setup_id
                )
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "contract",
                        "reason": "invalid_signal_contract",
                        "issues": contract_issues,
                    }
                )
                LOG.warning(
                    "invalid signal contract rejected | symbol=%s setup=%s direction=%s issues=%s",
                    signal.symbol,
                    signal.setup_id,
                    signal.direction,
                    contract_issues,
                )
                continue
            contract_validated_tracking_ids.add(signal.tracking_id)
            prepared = (
                prepared_by_tracking_id.get(signal.tracking_id) if prepared_by_tracking_id else None
            )
            limit_ready, limit_reason, limit_details = self._limit_entry_gate(signal, prepared)
            if not limit_ready:
                self._record_delivery_diag_reject(
                    "limit_entry",
                    limit_reason or "limit_entry_rejected",
                    setup_id=signal.setup_id,
                )
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "limit_entry",
                        "reason": limit_reason or "limit_entry_rejected",
                        "details": limit_details,
                    }
                )
                LOG.info(
                    "Signal rejected by limit entry gate | symbol=%s setup=%s "
                    "direction=%s reason=%s",
                    signal.symbol,
                    signal.setup_id,
                    signal.direction,
                    limit_reason,
                )
                continue
            gate_opts = self._confluence_gate_kwargs()
            gate_passed, confirmations, gate_details = self._hard_confluence_gate(
                signal,
                prepared,
                **gate_opts,
            )
            if not gate_passed:
                confluence_reason = str(gate_details.get("reason") or "hard_confluence_gate")
                self._record_delivery_diag_reject(
                    "confluence", confluence_reason, setup_id=signal.setup_id
                )
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "confluence",
                        "reason": confluence_reason,
                        "confirmations": confirmations,
                        "details": gate_details,
                    }
                )
                LOG.info(
                    (
                        "Signal rejected by hard confluence gate | symbol=%s setup=%s "
                        "direction=%s confirmations=%s details=%s"
                    ),
                    signal.symbol,
                    signal.setup_id,
                    signal.direction,
                    confirmations,
                    gate_details,
                )
                continue
            confluence_passed_tracking_ids.add(signal.tracking_id)
            self._bot.telemetry.append_jsonl(
                "gate_passed.jsonl",
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "symbol": signal.symbol,
                    "setup_id": signal.setup_id,
                    "direction": signal.direction,
                    "tracking_id": signal.tracking_id,
                    "confirmations": confirmations,
                    "details": gate_details,
                },
            )
            tier_decision = tier_by_tracking_id.get(signal.tracking_id)
            if tier_decision is None:
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "tier",
                        "reason": "tier_decision_missing",
                    }
                )
                continue
            if not tier_decision.allow:
                self._record_delivery_diag_reject(
                    "tier",
                    tier_decision.drop_reason or "tier_cap_rejected",
                    setup_id=signal.setup_id,
                )
                merge_meta = merge_meta_by_tracking_id.get(signal.tracking_id)
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "tier",
                        "reason": tier_decision.drop_reason or "tier_cap_rejected",
                        "tier": tier_decision.tier,
                        "tier_reason": tier_decision.reason,
                        "aligned_setup_ids": (
                            list(merge_meta.aligned_setup_ids) if merge_meta is not None else []
                        ),
                        "score_boost": merge_meta.score_boost if merge_meta is not None else 0.0,
                    }
                )
                drop_reason = tier_decision.drop_reason or "tier_cap_rejected"
                if drop_reason in {"action_cap_reached", "watch_cap_reached"}:
                    await notify_ops_tier_cap_starvation(
                        self._bot,
                        symbol=signal.symbol,
                        setup_id=signal.setup_id,
                        direction=str(signal.direction or ""),
                        tier=tier_decision.tier,
                        drop_reason=drop_reason,
                    )
                continue
            tier_allowed_tracking_ids.add(signal.tracking_id)
            if tier_decision.tier == "action" and r_class_blocks_action(
                signal.setup_id, self._bot.settings
            ):
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "tier",
                        "reason": "r_class_action_blocked",
                    }
                )
                continue
            if signal.setup_id in queued_setup_ids:
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "selection",
                        "reason": "setup_cycle_quota_filled",
                    }
                )
                continue
            is_blacklisted = await self._bot._modern_repo.is_symbol_blacklisted(
                signal.symbol,
                max_sl_streak=self._bot.settings.intelligence.max_consecutive_stop_losses,
                pause_hours=self._bot.settings.intelligence.stop_loss_pause_hours,
            )
            if is_blacklisted:
                sl_streak = await self._bot._modern_repo.get_consecutive_sl(signal.symbol)
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "memory",
                        "reason": "consecutive_sl_blacklist",
                        "consecutive_sl": sl_streak,
                    }
                )
                continue

            active_signals = await self._bot._modern_repo.get_active_signals(symbol=signal.symbol)
            existing_same_setup = next(
                (
                    r
                    for r in active_signals
                    if r.get("symbol") == signal.symbol
                    and r.get("setup_id") == signal.setup_id
                    and r.get("status") in ("pending", "active")
                ),
                None,
            )
            if existing_same_setup is not None:
                score_raw = (
                    existing_same_setup.get("score")
                    if isinstance(existing_same_setup, dict)
                    else getattr(existing_same_setup, "score", None)
                )
                existing_status = (
                    existing_same_setup.get("status")
                    if isinstance(existing_same_setup, dict)
                    else getattr(existing_same_setup, "status", None)
                )
                existing_direction = (
                    existing_same_setup.get("direction")
                    if isinstance(existing_same_setup, dict)
                    else getattr(existing_same_setup, "direction", None)
                )
                can_supersede_pending = str(existing_status) == "pending"
                better_score = (
                    score_raw is not None and signal.score >= float(score_raw or 0.0) + 0.10
                )
                direction_flip = str(existing_direction or "") != signal.direction
                if can_supersede_pending and (better_score or direction_flip):
                    if self._quality_monitor_rejects(signal, rejected_rows):
                        continue
                    closed = await self.close_superseded_signal(signal)
                    if closed:
                        await self.deliver_tracking(closed)
                    if signal.tracking_id not in contract_validated_tracking_ids:
                        msg = f"signal_contract validation was bypassed for {signal.tracking_id}"
                        raise ValueError(msg)
                    if signal.tracking_id not in confluence_passed_tracking_ids:
                        msg = f"hard confluence gate was bypassed for {signal.tracking_id}"
                        raise ValueError(msg)
                    if signal.tracking_id not in tier_allowed_tracking_ids:
                        msg = f"tier cap policy was bypassed for {signal.tracking_id}"
                        raise ValueError(msg)
                    self._queue_ready_signal(
                        signal,
                        portfolio_state=portfolio_state,
                        ready_to_send=ready_to_send,
                        queued_setup_ids=queued_setup_ids,
                        queued_symbol_direction=queued_symbol_direction,
                        rejected_rows=rejected_rows,
                    )
                else:
                    rejected_rows.append(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "symbol": signal.symbol,
                            "setup_id": signal.setup_id,
                            "direction": signal.direction,
                            "stage": "tracking",
                            "reason": "setup_has_open_signal",
                            "existing_direction": existing_direction,
                            "existing_status": existing_status,
                        }
                    )
                continue

            symbol_direction_key = self._symbol_direction_cooldown_key(signal)
            if symbol_direction_key in queued_symbol_direction:
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "cooldown",
                        "reason": "symbol_direction_cooldown_active",
                        "cooldown_minutes": self._bot.settings.filters.symbol_cooldown_minutes,
                    }
                )
                continue

            existing_symbol = next(
                (
                    r
                    for r in active_signals
                    if r.get("symbol") == signal.symbol and r.get("status") in ("pending", "active")
                ),
                None,
            )
            if existing_symbol is not None:
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "tracking",
                        "reason": "symbol_has_open_signal",
                        "existing_setup_id": existing_symbol.get("setup_id"),
                        "existing_direction": existing_symbol.get("direction"),
                        "existing_status": existing_symbol.get("status"),
                    }
                )
                continue
            is_symbol_direction_cooldown = await self._bot._modern_repo.is_cooldown_active(
                symbol_direction_key,
                self._bot.settings.filters.symbol_cooldown_minutes,
            )
            if is_symbol_direction_cooldown:
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "cooldown",
                        "reason": "symbol_direction_cooldown_active",
                        "cooldown_minutes": self._bot.settings.filters.symbol_cooldown_minutes,
                    }
                )
                continue

            family_key = self._family_cooldown_key(signal)
            family_minutes = int(getattr(self._bot.settings.filters, "family_cooldown_minutes", 0) or 0)
            if family_key in queued_family_keys:
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "cooldown",
                        "reason": "family_cooldown_queued",
                        "cooldown_minutes": family_minutes,
                    }
                )
                continue
            if family_minutes > 0:
                is_family_cooldown = await self._bot._modern_repo.is_cooldown_active(
                    family_key,
                    family_minutes,
                )
                if is_family_cooldown:
                    rejected_rows.append(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "symbol": signal.symbol,
                            "setup_id": signal.setup_id,
                            "direction": signal.direction,
                            "stage": "cooldown",
                            "reason": "family_cooldown_active",
                            "cooldown_minutes": family_minutes,
                        }
                    )
                    continue

            outcome_sl_minutes = int(
                getattr(self._bot.settings.filters, "outcome_sl_cooldown_minutes", 0) or 0
            )
            if outcome_sl_minutes > 0:
                blocked = False
                for outcome_key in (
                    f"outcome_sl:{signal.setup_id}:{signal.symbol}",
                    f"outcome_sl_family:{family_key.removeprefix('family:')}",
                ):
                    if await self._bot._modern_repo.is_cooldown_active(
                        outcome_key,
                        outcome_sl_minutes,
                    ):
                        blocked = True
                        break
                if blocked:
                    rejected_rows.append(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "symbol": signal.symbol,
                            "setup_id": signal.setup_id,
                            "direction": signal.direction,
                            "stage": "cooldown",
                            "reason": "outcome_sl_cooldown_active",
                            "cooldown_minutes": outcome_sl_minutes,
                        }
                    )
                    continue

            setup_interval = self._setup_interval_minutes(signal.setup_id)
            if setup_interval > 0:
                setup_interval_key = self._setup_interval_cooldown_key(signal.setup_id)
                is_setup_interval_cooldown = await self._bot._modern_repo.is_cooldown_active(
                    setup_interval_key,
                    setup_interval,
                )
                if is_setup_interval_cooldown:
                    rejected_rows.append(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "symbol": signal.symbol,
                            "setup_id": signal.setup_id,
                            "direction": signal.direction,
                            "stage": "cooldown",
                            "reason": "setup_interval_cooldown_active",
                            "cooldown_minutes": setup_interval,
                        }
                    )
                    continue

            cooldown_key = f"{signal.setup_id}:{signal.symbol}"
            is_cooldown_active = await self._bot._modern_repo.is_cooldown_active(
                cooldown_key,
                self._bot.settings.filters.cooldown_minutes,
            )
            if not is_cooldown_active:
                if self._quality_monitor_rejects(signal, rejected_rows):
                    continue
                if signal.tracking_id not in contract_validated_tracking_ids:
                    msg = f"signal_contract validation was bypassed for {signal.tracking_id}"
                    raise ValueError(msg)
                if signal.tracking_id not in confluence_passed_tracking_ids:
                    msg = f"hard confluence gate was bypassed for {signal.tracking_id}"
                    raise ValueError(msg)
                if signal.tracking_id not in tier_allowed_tracking_ids:
                    msg = f"tier cap policy was bypassed for {signal.tracking_id}"
                    raise ValueError(msg)
                self._queue_ready_signal(
                    signal,
                    portfolio_state=portfolio_state,
                    ready_to_send=ready_to_send,
                    queued_setup_ids=queued_setup_ids,
                    queued_symbol_direction=queued_symbol_direction,
                    rejected_rows=rejected_rows,
                    symbol_direction_key=symbol_direction_key,
                    queued_family_keys=queued_family_keys,
                    family_key=family_key,
                )
                continue

            if self._quality_monitor_rejects(signal, rejected_rows):
                continue
            closed = await self.close_superseded_signal(signal)
            if closed:
                await self.deliver_tracking(closed)
                if signal.tracking_id not in contract_validated_tracking_ids:
                    msg = f"signal_contract validation was bypassed for {signal.tracking_id}"
                    raise ValueError(msg)
                if signal.tracking_id not in confluence_passed_tracking_ids:
                    msg = f"hard confluence gate was bypassed for {signal.tracking_id}"
                    raise ValueError(msg)
                if signal.tracking_id not in tier_allowed_tracking_ids:
                    msg = f"tier cap policy was bypassed for {signal.tracking_id}"
                    raise ValueError(msg)
                self._queue_ready_signal(
                    signal,
                    portfolio_state=portfolio_state,
                    ready_to_send=ready_to_send,
                    queued_setup_ids=queued_setup_ids,
                    queued_symbol_direction=queued_symbol_direction,
                    rejected_rows=rejected_rows,
                    symbol_direction_key=symbol_direction_key,
                    queued_family_keys=queued_family_keys,
                    family_key=family_key,
                )
            else:
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "cooldown",
                        "reason": "cooldown_active",
                    }
                )

        if not ready_to_send:
            return [], rejected_rows, Counter(), merge_conflict_count

        delivered: list[Signal] = []
        delivery_status_counts: Counter[str] = Counter()

        market_ctx = await self._bot._modern_repo.get_market_context()
        btc_bias = market_ctx.get("btc_bias", "neutral")
        eth_bias = market_ctx.get("eth_bias", "neutral")

        for signal in ready_to_send:
            if signal.tracking_id not in contract_validated_tracking_ids:
                msg = f"signal_contract validation was bypassed for {signal.tracking_id}"
                raise ValueError(msg)
            if signal.tracking_id not in confluence_passed_tracking_ids:
                msg = f"hard confluence gate was bypassed for {signal.tracking_id}"
                raise ValueError(msg)
            if signal.tracking_id not in tier_allowed_tracking_ids:
                msg = f"tier cap policy was bypassed for {signal.tracking_id}"
                raise ValueError(msg)
            tier_decision = tier_by_tracking_id.get(signal.tracking_id)
            delivery_tier = tier_decision.tier if tier_decision is not None else "action"
            tier_reason = tier_decision.reason if tier_decision is not None else "score_action"
            session_cap = int(
                getattr(self._bot.settings.delivery, "action_cap_per_session", 0) or 0
            )
            session_used = int(getattr(self._bot, "_session_action_delivered", 0) or 0)
            if delivery_tier == "action" and session_cap > 0 and session_used >= session_cap:
                if _finite_score(signal.score) >= float(
                    self._bot.settings.delivery.watch_min_score
                ):
                    delivery_tier = "watch"
                    tier_reason = "action_session_cap_downgrade"
                else:
                    await self._bot.tracker.cancel_pending_delivery(signal)
                    self._record_delivery_diag_reject(
                        "tier",
                        "action_session_cap_reached",
                        setup_id=signal.setup_id,
                    )
                    rejected_rows.append(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "symbol": signal.symbol,
                            "setup_id": signal.setup_id,
                            "direction": signal.direction,
                            "stage": "tier",
                            "reason": "action_session_cap_reached",
                            "session_action_delivered": session_used,
                            "session_action_cap": session_cap,
                        }
                    )
                    continue
            burst_ok, burst_cap = self._burst_delivery_allows()
            if not burst_ok:
                await self._bot.tracker.cancel_pending_delivery(signal)
                self._record_delivery_diag_reject(
                    "delivery",
                    "burst_rate_limit",
                    setup_id=signal.setup_id,
                )
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "delivery",
                        "reason": "burst_rate_limit",
                        "max_signals_per_minute": burst_cap,
                    }
                )
                continue

            self._record_watch_screener(
                signal,
                tier=delivery_tier,
                tier_reason=tier_reason,
            )
            tier_map = {signal.tracking_id: delivery_tier}
            armed_ok, _ = await self._bot._wait_noncritical(
                label=f"journal {signal.symbol}/{signal.setup_id}",
                max_wait_s=self._bot._noncritical_timeout_seconds,
                operation=self._bot.tracker.arm_signals([signal], dry_run=False),
            )
            if not armed_ok:
                self._record_delivery_diag_reject(
                    "journal", "arm_timeout", setup_id=signal.setup_id
                )
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "journal",
                        "reason": "arm_timeout",
                    }
                )
                continue
            ok, results = await self._bot._wait_noncritical(
                label=f"deliver {signal.symbol}/{signal.setup_id}",
                max_wait_s=self._bot._delivery_timeout_seconds,
                operation=self._bot.delivery.deliver(
                    [signal],
                    dry_run=False,
                    btc_bias=btc_bias,
                    tier_by_tracking_id=tier_map,
                ),
            )
            if not ok or not results:
                await self._bot.tracker.cancel_pending_delivery(signal)
                continue

            for item in results:
                delivery_status_counts[item.status] += 1
                self._record_delivery_attempt(
                    item.signal,
                    status=item.status,
                    reason=item.reason,
                    message_id=item.message_id,
                )
                if item.status not in DELIVERY_SUCCESS_STATUSES:
                    await self._bot.tracker.cancel_pending_delivery(item.signal)
                    self._record_delivery_diag_reject(
                        "delivery",
                        f"delivery_{item.status}",
                        setup_id=item.signal.setup_id,
                    )
                    self._record_metrics_rejected(item.signal, f"delivery_{item.status}")
                    rejected_rows.append(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "symbol": item.signal.symbol,
                            "setup_id": item.signal.setup_id,
                            "direction": item.signal.direction,
                            "stage": "delivery",
                            "reason": f"delivery_{item.status}",
                            "delivery_reason": item.reason,
                        }
                    )
                    await notify_ops_delivery_failed(
                        self._bot,
                        symbol=item.signal.symbol,
                        setup_id=item.signal.setup_id,
                        direction=str(item.signal.direction or ""),
                        reason=f"delivery_{item.status}",
                        delivery_reason=item.reason,
                    )
                    LOG.info(
                        (
                            "delivery result not sent | status=%s reason=%s symbol=%s "
                            "setup=%s tracking_id=%s"
                        ),
                        item.status,
                        item.reason,
                        item.signal.symbol,
                        item.signal.setup_id,
                        item.signal.tracking_id,
                    )
                    continue
                delivered.append(item.signal)
                self._record_delivery_diag_delivered(item.signal.setup_id)
                self._record_metrics_delivered(item.signal)
                direction = str(item.signal.direction or "").strip().lower()
                if direction == "long":
                    self._bot._session_signals_long = (
                        int(getattr(self._bot, "_session_signals_long", 0) or 0) + 1
                    )
                elif direction == "short":
                    self._bot._session_signals_short = (
                        int(getattr(self._bot, "_session_signals_short", 0) or 0) + 1
                    )
                if delivery_tier == "action":
                    self._bot._session_action_delivered = (
                        int(getattr(self._bot, "_session_action_delivered", 0) or 0) + 1
                    )
                dashboard = getattr(self._bot, "dashboard", None)
                if dashboard is not None:
                    dashboard.notify_signal_delivered(item.signal)
                prepared = (
                    prepared_by_tracking_id.get(item.signal.tracking_id)
                    if prepared_by_tracking_id
                    else None
                )
                await self._bot.tracker.set_signal_features_async(
                    item.signal.tracking_id,
                    extract_features_from_signal(
                        item.signal,
                        prepared_data=build_prepared_feature_snapshot(prepared),
                    ),
                )
                cooldown_key = f"{item.signal.setup_id}:{item.signal.symbol}"
                await self._bot._modern_repo.set_cooldown(
                    cooldown_key,
                    datetime.now(UTC),
                    item.signal.setup_id,
                    item.signal.symbol,
                    "signal",
                )
                setup_interval = self._setup_interval_minutes(item.signal.setup_id)
                if setup_interval > 0:
                    await self._bot._modern_repo.set_cooldown(
                        self._setup_interval_cooldown_key(item.signal.setup_id),
                        datetime.now(UTC),
                        item.signal.setup_id,
                        None,
                        "setup_interval",
                    )
                if self._bot.settings.filters.symbol_cooldown_minutes > 0:
                    await self._bot._modern_repo.set_cooldown(
                        self._symbol_direction_cooldown_key(item.signal),
                        datetime.now(UTC),
                        item.signal.setup_id,
                        item.signal.symbol,
                        "symbol_direction",
                    )
                family_minutes = int(
                    getattr(self._bot.settings.filters, "family_cooldown_minutes", 0) or 0
                )
                if family_minutes > 0:
                    await self._bot._modern_repo.set_cooldown(
                        self._family_cooldown_key(item.signal),
                        datetime.now(UTC),
                        item.signal.setup_id,
                        item.signal.symbol,
                        "family",
                    )
                self._record_burst_delivery()
                if item.message_id is not None:
                    await self._bot._wait_noncritical(
                        label=f"link {item.signal.symbol}/{item.signal.setup_id}",
                        max_wait_s=self._bot._noncritical_timeout_seconds,
                        operation=self._bot.tracker.update_signal_message_ids(
                            {item.signal.tracking_id: item.message_id},
                            dry_run=False,
                        ),
                    )
                if delivery_tier == "watch":
                    await maybe_notify_watch_escalation(self._bot, item.signal, prepared)
                notifier_settings = getattr(self._bot.settings, "notifiers", None)
                if notifier_settings is not None:
                    if should_send_channel_analytics_companion(
                        notifier_settings,
                        tier=delivery_tier,
                    ):
                        task = asyncio.create_task(
                            self._bot.delivery.send_analytics_companion(
                                item.signal, btc_bias=btc_bias, eth_bias=eth_bias
                            ),
                            name=f"analytics:{item.signal.symbol}",
                        )
                        self._bot._background_tasks.add(task)
                        task.add_done_callback(self._bot._background_tasks.discard)
                    if delivery_tier == "watch":
                        task = asyncio.create_task(
                            send_operator_analytics_companion(
                                self._bot,
                                item.signal,
                                btc_bias=btc_bias,
                                eth_bias=eth_bias,
                            ),
                            name=f"watch_companion:{item.signal.symbol}",
                        )
                        self._bot._background_tasks.add(task)
                        task.add_done_callback(self._bot._background_tasks.discard)

        try:
            await self._bot.alerts.on_confirmed_signals(delivered, observed_at=datetime.now(UTC))
        except DEFENSIVE_EXC as exc:
            LOG.debug("alerts.on_confirmed_signals failed: %s", exc)
        if delivered:
            await self._bot._sync_ws_tracked_symbols()

        if direction_conflict_signals:
            conflict_delivered = await self._deliver_direction_conflict_watch(
                direction_conflict_signals,
                prepared_by_tracking_id=prepared_by_tracking_id,
                rejected_rows=rejected_rows,
                delivery_status_counts=delivery_status_counts,
                btc_bias=btc_bias,
                eth_bias=eth_bias,
            )
            delivered.extend(conflict_delivered)

        return delivered, rejected_rows, delivery_status_counts, merge_conflict_count

    async def deliver_tracking(self, events: list[SignalTrackingEvent]) -> None:
        """TP/SL tracking Telegram updates (post select_and_deliver contract path)."""
        outcome_map = {
            "tp1_hit": "tp1",
            "tp2_hit": "tp2",
            "stop_loss": "loss",
            "breakeven_stop": "breakeven_stop",
            "expired": "expired",
            "smart_exit": "smart_exit",
            "emergency_exit": "emergency_exit",
            "ambiguous_exit": "ambiguous_exit",
            "superseded": "superseded",
        }
        for event in events:
            outcome = outcome_map.get(event.event_type)
            if outcome:
                tracked = event.tracked
                if event.event_type == "breakeven_stop" or (
                    event.event_type == "stop_loss" and tracked.tp1_hit_at is not None
                ):
                    outcome = "breakeven_stop"
                regime = getattr(tracked, "regime_4h_confirmed", None) or "neutral"
                await self._bot._modern_repo.record_symbol_outcome(
                    tracked.symbol,
                    tracked.setup_id,
                    tracked.direction,
                    regime,
                    outcome,
                )
        await self._bot._sync_ws_tracked_symbols()
        await self._bot._wait_noncritical(
            label="tracking delivery",
            max_wait_s=self._bot._delivery_timeout_seconds,
            operation=self._bot.delivery.deliver_tracking_updates(events, dry_run=False),
        )
        sl_events = [
            event for event in events if event.event_type in {"stop_loss", "breakeven_stop"}
        ]
        if sl_events and bool(
            getattr(self._bot.settings.delivery, "sl_postmortem_to_operators", True)
        ):
            await self._send_sl_postmortem_to_operators(sl_events)
        dashboard = getattr(self._bot, "dashboard", None)
        if dashboard is not None and events:
            dashboard.notify_tracking_changed(event_count=len(events))

from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.domain.delivery_policy import r_class_blocks_action
from bot.domain.schemas import PreparedSymbol, Signal
from bot.persistence.outcomes import build_prepared_feature_snapshot, extract_features_from_signal
from bot.delivery.contract import validate_signal_contract
from bot.persistence.tracking import SignalTrackingEvent

if TYPE_CHECKING:
    from bot.runtime.bot import SignalBot


LOG = logging.getLogger("bot.runtime.bot")
MIN_CONFIRMATIONS = 3  # confirmations: ADR-003 hard confluence gate


class DeliveryOrchestrator:
    def __init__(self, bot: SignalBot) -> None:
        self._bot = bot

    @staticmethod
    def _rank_key(signal: Signal) -> tuple[float, float]:
        return (float(signal.score), float(signal.risk_reward or 0.0))

    @staticmethod
    def _apply_same_direction_confluence(signals: list[Signal]) -> Signal:
        ranked = sorted(signals, key=DeliveryOrchestrator._rank_key, reverse=True)
        best = ranked[0]
        return DeliveryOrchestrator._with_same_direction_confluence(best, ranked)

    @staticmethod
    def _with_same_direction_confluence(signal: Signal, peers: list[Signal]) -> Signal:
        ranked = sorted(peers, key=DeliveryOrchestrator._rank_key, reverse=True)
        setup_ids = sorted({signal.setup_id for signal in ranked})
        setup_count = len(setup_ids)
        if setup_count <= 1:
            return signal
        boost = min(0.05, 0.015 * float(setup_count - 1))
        reason = f"confluence_{setup_count}_setups"
        setup_reason = f"confluence_setups={','.join(setup_ids)}"
        reasons = list(signal.reasons)
        if reason not in reasons:
            reasons.append(reason)
        if setup_reason not in reasons:
            reasons.append(setup_reason)
        return replace(
            signal,
            score=round(min(1.0, float(signal.score) + boost), 4),
            reasons=tuple(reasons),
        )

    @staticmethod
    def _symbol_direction_cooldown_key(signal: Signal) -> str:
        return f"strategy_symbol_direction:{signal.symbol}:{signal.direction}"

    @staticmethod
    def _contract_issue_rows(signal: Signal) -> list[dict[str, object]]:
        return [issue.to_dict() for issue in validate_signal_contract(signal)]

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
    def _hard_confluence_gate(
        cls,
        signal: Signal,
        prepared: PreparedSymbol | None,
    ) -> tuple[bool, dict[str, bool], dict[str, object]]:
        """Require at least 3 independent confirmations before delivery."""
        if prepared is None:
            return (
                False,
                {
                    "trend": False,
                    "momentum": False,
                    "volume": False,
                    "htf": False,
                    "microstructure": False,
                },
                {"reason": "prepared_context_missing"},
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

        trend = False
        if None not in (close, ema20, ema50):
            if direction == "long":
                trend = bool(close > ema20 > ema50)
            elif direction == "short":
                trend = bool(close < ema20 < ema50)

        momentum = False
        if rsi is not None:
            if direction == "long":
                momentum = 30.0 < rsi < 65.0
            elif direction == "short":
                momentum = 35.0 < rsi < 70.0

        volume_ok = bool(
            volume is not None
            and volume_avg is not None
            and volume_avg > 0.0
            and volume > volume_avg * 1.2
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
        htf = True
        if direction == "long":
            htf = regime_1h != "downtrend" and regime_4h != "downtrend"
        elif direction == "short":
            htf = regime_1h != "uptrend" and regime_4h != "uptrend"
        else:
            htf = False

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
        microstructure = abs(funding_value) < 0.001 and abs(oi_value) < 12.0

        confirmations = {
            "trend": trend,
            "momentum": momentum,
            "volume": volume_ok,
            "htf": htf,
            "microstructure": microstructure,
        }
        confirmation_count = sum(confirmations.values())
        details: dict[str, object] = {
            "confirmed": confirmation_count,
            "required": MIN_CONFIRMATIONS,
            "close": close,
            "ema20": ema20,
            "ema50": ema50,
            "rsi14": rsi,
            "volume": volume,
            "volume_mean20": volume_avg,
            "regime_1h": regime_1h,
            "regime_4h": regime_4h,
            "funding_rate": funding_value,
            "oi_change_pct": oi_value,
        }
        return confirmation_count >= MIN_CONFIRMATIONS, confirmations, details

    def _record_delivery_attempt(
        self,
        signal: Signal,
        *,
        status: str,
        reason: str | None,
        message_id: int | None,
    ) -> None:
        telemetry = getattr(self._bot, "telemetry", None)
        append_jsonl = getattr(telemetry, "append_jsonl", None)
        if not callable(append_jsonl):
            return
        append_jsonl(
            "delivery.jsonl",
            {
                "ts": datetime.now(UTC).isoformat(),
                **signal.to_log_row(),
                "delivery_status": status,
                "delivery_reason": reason,
                "message_id": message_id,
            },
        )

    def select_and_rank(
        self, all_candidates: dict[str, list[Signal]], max_signals: int
    ) -> list[Signal]:
        flat_candidates: list[Signal] = []
        for symbol_candidates in all_candidates.values():
            flat_candidates.extend(symbol_candidates)
        if not flat_candidates:
            return []

        same_direction: dict[tuple[str, str], list[Signal]] = {}
        for signal in flat_candidates:
            same_direction.setdefault((signal.symbol, signal.direction), []).append(signal)

        by_setup: dict[str, list[Signal]] = {}
        for signal in sorted(flat_candidates, key=self._rank_key, reverse=True):
            by_setup.setdefault(signal.setup_id, []).append(signal)

        selected: list[Signal] = []
        selected_keys: set[str] = set()
        selected_symbols: set[str] = set()

        def _enriched(signal: Signal) -> Signal:
            peers = same_direction.get((signal.symbol, signal.direction), [signal])
            return self._with_same_direction_confluence(signal, peers)

        setup_lanes = sorted(
            by_setup.values(),
            key=lambda items: self._rank_key(items[0]) if items else (0.0, 0.0),
            reverse=True,
        )
        for setup_signals in setup_lanes:
            if len(selected) >= max_signals:
                break
            for signal in setup_signals:
                key = signal.signal_key
                if key in selected_keys or signal.symbol in selected_symbols:
                    continue
                selected.append(_enriched(signal))
                selected_keys.add(key)
                selected_symbols.add(signal.symbol)
                break

        for signal in sorted(flat_candidates, key=self._rank_key, reverse=True):
            if len(selected) >= max_signals:
                break
            key = signal.signal_key
            if key in selected_keys or signal.symbol in selected_symbols:
                continue
            selected.append(_enriched(signal))
            selected_keys.add(key)
            selected_symbols.add(signal.symbol)

        LOG.debug(
            "select_and_rank | candidates=%d symbols=%d setups=%d selected=%d setup_first=true",
            len(flat_candidates),
            len({signal.symbol for signal in flat_candidates}),
            len(by_setup),
            len(selected),
        )
        return selected

    async def close_superseded_signal(self, new_signal: Signal) -> list[SignalTrackingEvent] | None:
        try:
            return await self._bot.tracker.supersede_open_signal(new_signal, dry_run=False)
        except Exception as exc:
            LOG.debug("supersede failed for %s: %s", new_signal.symbol, exc)
            return None

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
            "quality monitor paused delivery | symbol=%s setup=%s recommendation=%s consecutive_losses=%s win_rate=%s",
            signal.symbol,
            signal.setup_id,
            health.get("recommendation"),
            health.get("consecutive_losses"),
            health.get("win_rate"),
        )
        return True

    async def deliver_tracking(self, events: list[SignalTrackingEvent]) -> None:
        outcome_map = {
            "tp1_hit": "tp1",
            "tp2_hit": "tp2",
            "stop_loss": "loss",
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
                if event.event_type == "stop_loss" and tracked.tp1_hit_at is not None:
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
            timeout=self._bot._delivery_timeout_seconds,
            operation=self._bot.delivery.deliver_tracking_updates(events, dry_run=False),
        )

    async def select_and_deliver(
        self,
        signals: list[Signal],
        *,
        prepared_by_tracking_id: dict[str, PreparedSymbol] | None = None,
    ) -> tuple[list[Signal], list[dict[str, Any]], Counter[str]]:
        if not signals:
            return [], [], Counter()

        from .merge import merge_candidates

        signals = [meta.primary for meta in merge_candidates(signals)]

        ready_to_send: list[Signal] = []
        rejected_rows: list[dict[str, Any]] = []
        queued_symbol_direction: set[str] = set()
        queued_setup_ids: set[str] = set()
        contract_validated_tracking_ids: set[str] = set()
        confluence_passed_tracking_ids: set[str] = set()

        for signal in signals:
            contract_issues = self._contract_issue_rows(signal)
            contract_validated_tracking_ids.add(signal.tracking_id)
            if signal.tracking_id not in contract_validated_tracking_ids:
                raise ValueError(
                    f"signal_contract validation was bypassed for {signal.tracking_id}"
                )
            if contract_issues:
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
            prepared = (
                prepared_by_tracking_id.get(signal.tracking_id)
                if prepared_by_tracking_id
                else None
            )
            gate_passed, confirmations, gate_details = self._hard_confluence_gate(signal, prepared)
            if not gate_passed:
                rejected_rows.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "stage": "confluence",
                        "reason": "hard_confluence_gate_failed",
                        "confirmations": confirmations,
                        "details": gate_details,
                    }
                )
                LOG.info(
                    "Signal rejected by hard confluence gate | symbol=%s setup=%s direction=%s confirmations=%s details=%s",
                    signal.symbol,
                    signal.setup_id,
                    signal.direction,
                    confirmations,
                    gate_details,
                )
                continue
            confluence_passed_tracking_ids.add(signal.tracking_id)
            if r_class_blocks_action(signal.setup_id, self._bot.settings):
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
                better_score = score_raw is not None and signal.score >= float(score_raw or 0.0) + 0.10
                direction_flip = str(existing_direction or "") != signal.direction
                if can_supersede_pending and (better_score or direction_flip):
                    if self._quality_monitor_rejects(signal, rejected_rows):
                        continue
                    closed = await self.close_superseded_signal(signal)
                    if closed:
                        await self.deliver_tracking(closed)
                    if signal.tracking_id not in contract_validated_tracking_ids:
                        raise ValueError(
                            f"signal_contract validation was bypassed for {signal.tracking_id}"
                        )
                    if signal.tracking_id not in confluence_passed_tracking_ids:
                        raise ValueError(
                            f"hard confluence gate was bypassed for {signal.tracking_id}"
                        )
                    ready_to_send.append(signal)
                    queued_setup_ids.add(signal.setup_id)
                    queued_symbol_direction.add(self._symbol_direction_cooldown_key(signal))
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
                    if r.get("symbol") == signal.symbol
                    and r.get("status") in ("pending", "active")
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

            cooldown_key = f"{signal.setup_id}:{signal.symbol}"
            is_cooldown_active = await self._bot._modern_repo.is_cooldown_active(
                cooldown_key,
                self._bot.settings.filters.cooldown_minutes,
            )
            if not is_cooldown_active:
                if self._quality_monitor_rejects(signal, rejected_rows):
                    continue
                if signal.tracking_id not in contract_validated_tracking_ids:
                    raise ValueError(
                        f"signal_contract validation was bypassed for {signal.tracking_id}"
                    )
                if signal.tracking_id not in confluence_passed_tracking_ids:
                    raise ValueError(f"hard confluence gate was bypassed for {signal.tracking_id}")
                ready_to_send.append(signal)
                queued_setup_ids.add(signal.setup_id)
                queued_symbol_direction.add(symbol_direction_key)
                continue

            if self._quality_monitor_rejects(signal, rejected_rows):
                continue
            closed = await self.close_superseded_signal(signal)
            if closed:
                await self.deliver_tracking(closed)
                if signal.tracking_id not in contract_validated_tracking_ids:
                    raise ValueError(
                        f"signal_contract validation was bypassed for {signal.tracking_id}"
                    )
                if signal.tracking_id not in confluence_passed_tracking_ids:
                    raise ValueError(f"hard confluence gate was bypassed for {signal.tracking_id}")
                ready_to_send.append(signal)
                queued_setup_ids.add(signal.setup_id)
                queued_symbol_direction.add(symbol_direction_key)
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
            return [], rejected_rows, Counter()

        delivered: list[Signal] = []
        delivery_status_counts: Counter[str] = Counter()

        market_ctx = await self._bot._modern_repo.get_market_context()
        btc_bias = market_ctx.get("btc_bias", "neutral")
        eth_bias = market_ctx.get("eth_bias", "neutral")

        for signal in ready_to_send:
            if signal.tracking_id not in contract_validated_tracking_ids:
                raise ValueError(
                    f"signal_contract validation was bypassed for {signal.tracking_id}"
                )
            if signal.tracking_id not in confluence_passed_tracking_ids:
                raise ValueError(f"hard confluence gate was bypassed for {signal.tracking_id}")
            ok, results = await self._bot._wait_noncritical(
                label=f"deliver {signal.symbol}/{signal.setup_id}",
                timeout=self._bot._delivery_timeout_seconds,
                operation=self._bot.delivery.deliver([signal], dry_run=False, btc_bias=btc_bias),
            )
            if not ok or not results:
                continue

            for item in results:
                delivery_status_counts[item.status] += 1
                self._record_delivery_attempt(
                    item.signal,
                    status=item.status,
                    reason=item.reason,
                    message_id=item.message_id,
                )
                if item.status != "sent":
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
                    LOG.info(
                        "delivery result not sent | status=%s reason=%s symbol=%s setup=%s tracking_id=%s",
                        item.status,
                        item.reason,
                        item.signal.symbol,
                        item.signal.setup_id,
                        item.signal.tracking_id,
                    )
                    continue
                delivered.append(item.signal)
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
                if self._bot.settings.filters.symbol_cooldown_minutes > 0:
                    await self._bot._modern_repo.set_cooldown(
                        self._symbol_direction_cooldown_key(item.signal),
                        datetime.now(UTC),
                        item.signal.setup_id,
                        item.signal.symbol,
                        "symbol_direction",
                    )
                await self._bot._wait_noncritical(
                    label=f"arm {item.signal.symbol}/{item.signal.setup_id}",
                    timeout=self._bot._noncritical_timeout_seconds,
                    operation=self._bot.tracker.arm_signals_with_messages(
                        [item.signal],
                        dry_run=False,
                        message_ids={item.signal.tracking_id: item.message_id},
                    ),
                )
                notifier_settings = getattr(self._bot.settings, "notifiers", None)
                if bool(getattr(notifier_settings, "send_analytics_companion", False)):
                    task = asyncio.create_task(
                        self._bot.delivery.send_analytics_companion(
                            item.signal, btc_bias=btc_bias, eth_bias=eth_bias
                        ),
                        name=f"analytics:{item.signal.symbol}",
                    )
                    self._bot._background_tasks.add(task)
                    task.add_done_callback(self._bot._background_tasks.discard)

        try:
            await self._bot.alerts.on_confirmed_signals(delivered, observed_at=datetime.now(UTC))
        except Exception as exc:
            LOG.debug("alerts.on_confirmed_signals failed: %s", exc)
        if delivered:
            await self._bot._sync_ws_tracked_symbols()

        return delivered, rejected_rows, delivery_status_counts

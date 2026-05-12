from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.domain.schemas import PreparedSymbol, Signal
from bot.outcomes import build_prepared_feature_snapshot, extract_features_from_signal
from bot.tracking import SignalTrackingEvent

if TYPE_CHECKING:
    from bot.application.bot import SignalBot


LOG = logging.getLogger("bot.application.bot")


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
        setup_ids = sorted({signal.setup_id for signal in ranked})
        setup_count = len(setup_ids)
        if setup_count <= 1:
            return best
        boost = min(0.05, 0.015 * float(setup_count - 1))
        reason = f"confluence_{setup_count}_setups"
        setup_reason = f"confluence_setups={','.join(setup_ids)}"
        reasons = list(best.reasons)
        if reason not in reasons:
            reasons.append(reason)
        if setup_reason not in reasons:
            reasons.append(setup_reason)
        return replace(
            best,
            score=round(min(1.0, float(best.score) + boost), 4),
            reasons=tuple(reasons),
        )

    @staticmethod
    def _symbol_direction_cooldown_key(signal: Signal) -> str:
        return f"symbol_direction:{signal.symbol}:{signal.direction}"

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
            same_direction.setdefault((signal.symbol, signal.direction), []).append(
                signal
            )

        best_by_direction = [
            self._apply_same_direction_confluence(signals)
            for signals in same_direction.values()
        ]
        best_by_symbol: dict[str, Signal] = {}
        for signal in sorted(best_by_direction, key=self._rank_key, reverse=True):
            best_by_symbol.setdefault(signal.symbol, signal)

        ranked = sorted(best_by_symbol.values(), key=self._rank_key, reverse=True)
        selected = ranked[:max_signals]
        LOG.debug(
            "select_and_rank | candidates=%d deduped_symbols=%d selected=%d",
            len(flat_candidates),
            len(best_by_symbol),
            len(selected),
        )
        return selected

    async def close_superseded_signal(
        self, new_signal: Signal
    ) -> list[SignalTrackingEvent] | None:
        try:
            return await self._bot.tracker.supersede_open_signal(
                new_signal, dry_run=False
            )
        except Exception as exc:
            LOG.debug("supersede failed for %s: %s", new_signal.symbol, exc)
            return None

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
            operation=self._bot.delivery.deliver_tracking_updates(
                events, dry_run=False
            ),
        )

    async def select_and_deliver(
        self,
        signals: list[Signal],
        *,
        prepared_by_tracking_id: dict[str, PreparedSymbol] | None = None,
    ) -> tuple[list[Signal], list[dict[str, Any]], Counter[str]]:
        if not signals:
            return [], [], Counter()

        ready_to_send: list[Signal] = []
        rejected_rows: list[dict[str, Any]] = []
        queued_symbol_direction: set[str] = set()

        for signal in signals:
            is_blacklisted = await self._bot._modern_repo.is_symbol_blacklisted(
                signal.symbol,
                max_sl_streak=self._bot.settings.intelligence.max_consecutive_stop_losses,
                pause_hours=self._bot.settings.intelligence.stop_loss_pause_hours,
            )
            if is_blacklisted:
                sl_streak = await self._bot._modern_repo.get_consecutive_sl(
                    signal.symbol
                )
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

            active_signals = await self._bot._modern_repo.get_active_signals(
                symbol=signal.symbol
            )
            existing = next(
                (
                    r
                    for r in active_signals
                    if r.get("symbol") == signal.symbol
                    and r.get("status") in ("pending", "active")
                ),
                None,
            )
            if existing is not None:
                score_raw = (
                    existing.get("score")
                    if isinstance(existing, dict)
                    else getattr(existing, "score", None)
                )
                if (
                    score_raw is not None
                    and signal.score >= float(score_raw or 0.0) + 0.10
                ):
                    closed = await self.close_superseded_signal(signal)
                    if closed:
                        await self.deliver_tracking(closed)
                    ready_to_send.append(signal)
                    queued_symbol_direction.add(
                        self._symbol_direction_cooldown_key(signal)
                    )
                else:
                    rejected_rows.append(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "symbol": signal.symbol,
                            "setup_id": signal.setup_id,
                            "direction": signal.direction,
                            "stage": "tracking",
                            "reason": "symbol_has_open_signal",
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
                ready_to_send.append(signal)
                queued_symbol_direction.add(symbol_direction_key)
                continue

            closed = await self.close_superseded_signal(signal)
            if closed:
                await self.deliver_tracking(closed)
                ready_to_send.append(signal)
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

        delivered: list[Signal] = []
        delivery_status_counts: Counter[str] = Counter()

        market_ctx = await self._bot._modern_repo.get_market_context()
        btc_bias = market_ctx.get("btc_bias", "neutral")
        eth_bias = market_ctx.get("eth_bias", "neutral")

        for signal in ready_to_send:
            ok, results = await self._bot._wait_noncritical(
                label=f"deliver {signal.symbol}/{signal.setup_id}",
                timeout=self._bot._delivery_timeout_seconds,
                operation=self._bot.delivery.deliver(
                    [signal], dry_run=False, btc_bias=btc_bias
                ),
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
                    LOG.warning(
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
                asyncio.create_task(
                    self._bot.delivery.send_analytics_companion(
                        item.signal, btc_bias=btc_bias, eth_bias=eth_bias
                    ),
                    name=f"analytics:{item.signal.symbol}",
                )

        try:
            await self._bot.alerts.on_confirmed_signals(
                delivered, observed_at=datetime.now(UTC)
            )
        except Exception as exc:
            LOG.debug("alerts.on_confirmed_signals failed: %s", exc)
        if delivered:
            await self._bot._sync_ws_tracked_symbols()

        return delivered, rejected_rows, delivery_status_counts

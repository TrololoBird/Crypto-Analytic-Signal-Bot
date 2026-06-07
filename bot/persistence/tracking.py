"""Signal tracking module for monitoring signal lifecycle and outcomes.

Domain lifecycle service: orchestrates pending/active/closed state and Telegram edits.
SQL CRUD for ``active_signals`` / ``signal_outcomes`` lives in ``MemoryRepository``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import tempfile
import time
import typing
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bot.domain.strategy_catalog import catalog_setup_family
from bot.persistence._tracking_review import _price_in_entry_zone
from bot.persistence.tracking_events import SignalTrackingEvent
from bot.runtime.errors import DEFENSIVE_EXC

from ..domain.limit_entry import (
    DEFAULT_ENTRY_ORDER_TYPE,
)
from ..persistence.tracked import (
    TrackedSignalState,
    parse_state_dt,
    resolve_terminal_close_reason,
    tp1_reached_from_excursion,
)
from .outcomes import SignalFeatures, SignalOutcome, create_outcome_from_tracked

if typing.TYPE_CHECKING:

    class _SignalTrackerBases:
        @staticmethod
        def _update_price_excursion(
            tracked: TrackedSignalState,
            price: float | None,
        ) -> None: ...

        async def arm_signals_with_messages(
            self,
            signals: list[Signal],
            *,
            dry_run: bool,
            message_ids: dict[str, int] | None = None,
        ) -> list[SignalTrackingEvent]: ...

        async def _capture_post_sl_recovery(self, tracked: TrackedSignalState) -> None: ...
else:
    from bot.persistence._tracking_review import TPSLReviewMixin
    from bot.persistence._tracking_telegram import TelegramTrackingMixin

    class _SignalTrackerBases(TPSLReviewMixin, TelegramTrackingMixin):
        pass


__all__ = ["SignalTracker", "SignalTrackingEvent"]

if typing.TYPE_CHECKING:
    from ..diagnostics.facade import SignalQualityMonitor
    from ..domain.config import BotSettings
    from ..domain.schemas import Signal
    from ..telemetry import TelemetryStore

    class MemoryRepository:
        async def cleanup_signal_outcomes_before(self, cutoff_iso: str) -> int: ...

        async def get_signal_outcomes(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]: ...

        async def record_setup_outcome(self, *args: Any, **kwargs: Any) -> None: ...

        async def get_active_signals(
            self, *args: Any, **kwargs: Any
        ) -> list[TrackedSignalState]: ...

        async def save_signal_outcomes_batch(self, *args: Any, **kwargs: Any) -> None: ...

        async def get_tracking_stats(self, *args: Any, **kwargs: Any) -> dict[str, int]: ...

        async def save_active_signal(self, *args: Any, **kwargs: Any) -> None: ...

        async def increment_tracking_stats(self, *args: Any, **kwargs: Any) -> None: ...

        async def save_signal_outcome(self, *args: Any, **kwargs: Any) -> None: ...


LOG = logging.getLogger("bot.tracking")


class SignalTracker(_SignalTrackerBases):
    def __init__(
        self,
        settings: BotSettings,
        *,
        market_data: Any,
        telemetry: TelemetryStore,
        features_store: dict[str, SignalFeatures] | None = None,
        memory_repo: MemoryRepository,
        quality_monitor: SignalQualityMonitor | None = None,
    ) -> None:
        self.settings = settings
        self.market_data = market_data
        self.telemetry = telemetry
        self.memory_repo = memory_repo
        self.quality_monitor = quality_monitor
        self._features_file: Path | None = getattr(settings, "features_store_file", None)
        if features_store is not None:
            self.features_store = features_store
        else:
            self.features_store = self._load_features_store()

        # Pending outcomes queue for batching I/O operations
        self._pending_outcomes: list[dict[str, Any]] = []
        self._pending_outcomes_lock = asyncio.Lock()
        self._pending_outcomes_flush_size = 1  # Flush immediately — avoid losing outcomes on crash
        self._last_outcome_cleanup_ts: float = 0.0
        self._features_persist_lock = asyncio.Lock()
        self._symbol_review_locks: dict[str, asyncio.Lock] = {}
        self._symbol_review_durations: dict[str, deque[float]] = {}
        # In-memory trailing stops: tracking_id -> current stop level
        self._trailing_stops: dict[str, float] = {}
        # Pace REST aggTrade backfill to avoid weight spikes at review/startup time
        self._agg_trade_semaphore = asyncio.Semaphore(2)
        self._agg_trade_min_gap_s = 0.12
        self._agg_trade_startup_gap_s = 0.35
        self._last_agg_trade_fetch_mono = 0.0

    def _symbol_review_lock(self, symbol: str) -> asyncio.Lock:
        key = str(symbol or "").upper()
        lock = self._symbol_review_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._symbol_review_locks[key] = lock
        return lock

    def _cleanup_symbol_review_locks(self, active_symbols: set[str]) -> None:
        if len(self._symbol_review_locks) <= 200:
            return
        active = {str(symbol or "").upper() for symbol in active_symbols}
        stale = [symbol for symbol in self._symbol_review_locks if symbol not in active]
        removed = 0
        for symbol in stale:
            lock = self._symbol_review_locks.get(symbol)
            if lock is not None and lock.locked():
                continue
            self._symbol_review_locks.pop(symbol, None)
            self._symbol_review_durations.pop(symbol, None)
            removed += 1
        if removed:
            LOG.debug(
                "pruned %d stale symbol review locks | remaining=%d",
                removed,
                len(self._symbol_review_locks),
            )

    def _record_symbol_review_duration(
        self,
        symbol: str,
        *,
        elapsed: float,
        tracked_count: int,
    ) -> None:
        key = str(symbol or "").upper()
        durations = self._symbol_review_durations.get(key)
        if durations is None:
            durations = deque(maxlen=100)
            self._symbol_review_durations[key] = durations
        durations.append(elapsed)
        if elapsed > 15.0:
            LOG.info(
                (
                    "slow symbol tracking review | symbol=%s elapsed=%.3fs tracked_rows=%d "
                    "avg_100=%.3fs"
                ),
                symbol,
                elapsed,
                tracked_count,
                sum(durations) / max(len(durations), 1),
            )

    async def _flush_pending_outcomes(self) -> None:
        """Flush pending outcomes to disk in batch."""
        if not self._pending_outcomes:
            return

        async with self._pending_outcomes_lock:
            outcomes_to_flush = self._pending_outcomes[:]
            self._pending_outcomes.clear()

        if outcomes_to_flush:
            try:
                await self.memory_repo.save_signal_outcomes_batch(outcomes_to_flush)
            except DEFENSIVE_EXC:
                LOG.exception("batch outcome flush failed")

    def _load_features_store(self) -> dict[str, SignalFeatures]:
        """Load persisted features from disk. Returns empty dict on any error."""
        if not self._features_file or not self._features_file.exists():
            return {}
        try:
            data = json.loads(self._features_file.read_text(encoding="utf-8"))
            result: dict[str, SignalFeatures] = {}
            for tid, fdict in data.items():
                try:
                    result[tid] = SignalFeatures(**fdict)
                except (ValueError, TypeError) as exc:
                    LOG.debug("features_store entry skipped | tracking_id=%s error=%s", tid, exc)
            if result:
                LOG.info("features_store loaded | entries=%d", len(result))
        except DEFENSIVE_EXC as exc:
            LOG.debug("features_store load failed: %s", exc)
            return {}
        else:
            return result

    def _persist_features_store(self) -> None:
        """Persist features to disk so they survive bot restarts."""
        if not self._features_file:
            return
        tmp_path: str | None = None
        try:
            self._features_file.parent.mkdir(parents=True, exist_ok=True)
            data = {tid: f.to_dict() for tid, f in self.features_store.items()}
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._features_file.parent,
                prefix=f"{self._features_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = handle.name
                handle.write(json.dumps(data, indent=2))
                handle.flush()
            Path(tmp_path).replace(self._features_file)
        except DEFENSIVE_EXC as exc:
            LOG.debug("features_store persist failed: %s", exc)
            if tmp_path:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink(missing_ok=True)

    async def _persist_features_store_async(self) -> None:
        if not self._features_file:
            return
        async with self._features_persist_lock:
            await asyncio.to_thread(self._persist_features_store)

    def set_signal_features(self, tracking_id: str, features: SignalFeatures) -> None:
        """Сохраняет признаки сигнала для последующей записи в outcome."""
        self.features_store[tracking_id] = features
        self._persist_features_store()

    async def set_signal_features_async(self, tracking_id: str, features: SignalFeatures) -> None:
        """Async runtime path that avoids blocking the event loop on file I/O."""
        self.features_store[tracking_id] = features
        await self._persist_features_store_async()

    def _tracked_to_payload(self, tracked: TrackedSignalState) -> dict[str, Any]:
        fields = getattr(TrackedSignalState, "__struct_fields__", ())
        payload = {field: getattr(tracked, field, None) for field in fields}
        payload["reasons"] = list(tracked.reasons)
        return payload

    def _tracked_from_payload(self, payload: dict[str, Any]) -> TrackedSignalState:
        row = dict(payload)
        if "reasons" in row and isinstance(row["reasons"], list):
            row["reasons"] = tuple(row["reasons"])
        row.setdefault("initial_stop", row.get("stop"))
        row.setdefault("stop_price", row.get("stop"))
        row.setdefault("max_favorable_pct", 0.0)
        row.setdefault("max_adverse_pct", 0.0)
        if row.get("tp1_hit_at") and row.get("tp1_price") is None:
            row["tp1_price"] = row.get("take_profit_1")
        if row.get("tp2_hit_at") and row.get("tp2_price") is None:
            row["tp2_price"] = row.get("take_profit_2")
        row.setdefault("valid_until", row.get("pending_expires_at"))
        row.setdefault("scale_weights", (0.5, 0.3, 0.2))
        if isinstance(row.get("scale_weights"), list):
            row["scale_weights"] = tuple(row["scale_weights"])
        weights = row.get("scale_weights")
        if not isinstance(weights, tuple) or len(weights) != 3:
            row["scale_weights"] = (0.5, 0.3, 0.2)
        else:
            try:
                normalized_weights = tuple(float(weight) for weight in weights)
            except TypeError, ValueError:
                normalized_weights = (0.5, 0.3, 0.2)
            if len(normalized_weights) != 3 or not all(
                math.isfinite(weight) and weight >= 0.0 for weight in normalized_weights
            ):
                normalized_weights = (0.5, 0.3, 0.2)
            row["scale_weights"] = normalized_weights
        row.setdefault("ttl_bars", None)
        row.setdefault("single_target_mode", False)
        row.setdefault("target_integrity_status", "unchecked")
        row.setdefault("entry_order_type", DEFAULT_ENTRY_ORDER_TYPE)
        row.setdefault("confirmation_profile", "trend_follow")
        row.setdefault("entry_zone_touched_at", None)
        row.setdefault("entry_confirm_pending_at", None)
        row.setdefault("last_lifecycle_note", None)
        row.setdefault("trailing_stop", row.get("trailing_stop"))
        row["single_target_mode"] = bool(row.get("single_target_mode"))
        return TrackedSignalState(**row)

    async def _stats_snapshot(self) -> dict[str, int]:
        stats = await self.memory_repo.get_tracking_stats()
        return {str(key): int(value) for key, value in stats.items()}

    async def _active_signals(self, *, symbol: str | None = None) -> list[TrackedSignalState]:
        rows = await self.memory_repo.get_active_signals(symbol=symbol)
        states = [self._tracked_from_payload(row) for row in rows]
        for tracked in states:
            if tracked.status != "active":
                continue
            stop_px = tracked.stop
            if stop_px is not None and float(stop_px) > 0.0:
                self._trailing_stops.setdefault(tracked.tracking_id, float(stop_px))
            trail_col = getattr(tracked, "trailing_stop", None)
            if trail_col is not None and float(trail_col) > 0.0:
                self._trailing_stops[tracked.tracking_id] = float(trail_col)
        return states

    def _apply_trailing_stop(self, tracked: TrackedSignalState, stop_price: float) -> None:
        """Sync in-memory and struct trailing stop; caller persists when ready.

        п.38 — Never widen the stop beyond the initial level.
        """
        if stop_price <= 0.0:
            return
        initial_stop = float(tracked.stop)
        if initial_stop > 0.0:
            if tracked.direction == "long" and stop_price < initial_stop:
                return
            if tracked.direction == "short" and stop_price > initial_stop:
                return
        self._trailing_stops[tracked.tracking_id] = float(stop_price)
        if bool(getattr(self.settings.tracking, "persist_trailing_stop", True)):
            tracked.trailing_stop = float(stop_price)
            tracked.stop = float(stop_price)
            tracked.stop_price = float(stop_price)

    async def _persist_tracking_state(self) -> None:
        # Tracking rows are persisted incrementally via MemoryRepository calls.
        # Here we only flush pending batched outcomes so callers that expect
        # an explicit "persist" step do not lose queued outcomes on shutdown.
        await self._flush_pending_outcomes()
        retention_days = int(getattr(self.settings.tracking, "outcome_retention_days", 90) or 90)
        await self._cleanup_old_outcomes(retention_days)

    async def persist_tracking_state(self) -> None:
        """Flush pending tracking writes before shutdown."""
        await self._persist_tracking_state()

    async def _cleanup_old_outcomes(self, retention_days: int = 90) -> None:
        if retention_days <= 0:
            return
        now_ts = time.monotonic()
        if now_ts - self._last_outcome_cleanup_ts < 3600.0:
            return
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        try:
            deleted = await self.memory_repo.cleanup_signal_outcomes_before(cutoff)
            if deleted > 0:
                LOG.info(
                    "tracking cleanup removed old outcomes | deleted=%d retention_days=%d",
                    deleted,
                    retention_days,
                )
        except DEFENSIVE_EXC as exc:
            LOG.debug("tracking cleanup failed (non-fatal): %s", exc)
        finally:
            self._last_outcome_cleanup_ts = now_ts

    def _fallback_features_for_tracked(self, tracked: TrackedSignalState) -> SignalFeatures:
        return SignalFeatures(
            base_score=tracked.score,
            llm_verdict=None,
            risk_reward=tracked.risk_reward,
            stop_distance_pct=self._tracked_stop_distance_pct(tracked),
            entry_mid=tracked.entry_mid,
            setup_id=tracked.setup_id,
            direction=tracked.direction,
            timeframe=tracked.timeframe,
            bias_4h=tracked.bias_4h,
        )

    @staticmethod
    def _tracked_stop_distance_pct(tracked: TrackedSignalState) -> float:
        entry_price = tracked.entry_mid
        risk_stop = tracked.initial_stop if tracked.initial_stop is not None else tracked.stop
        if not entry_price or not risk_stop:
            return 0.0
        try:
            entry = float(entry_price)
            stop = float(risk_stop)
        except TypeError, ValueError:
            return 0.0
        if entry <= 0.0 or not math.isfinite(entry) or not math.isfinite(stop):
            return 0.0
        return abs(entry - stop) / entry * 100.0

    def _build_outcome_payload(self, tracked: TrackedSignalState) -> dict[str, Any]:
        features = self.features_store.get(tracked.tracking_id)
        if not features:
            features = self._fallback_features_for_tracked(tracked)

        entry_price = tracked.activation_price or tracked.entry_mid
        max_profit_pct = float(tracked.max_favorable_pct or 0.0)
        max_loss_pct = float(tracked.max_adverse_pct or 0.0)
        if entry_price and entry_price > 0.0 and max_profit_pct == 0.0 and max_loss_pct == 0.0:
            direction_sign = 1.0 if tracked.direction == "long" else -1.0
            if tracked.tp1_hit_at and tracked.tp1_price is not None:
                max_profit_pct = max(
                    max_profit_pct,
                    direction_sign * (tracked.tp1_price - entry_price) / entry_price * 100.0,
                )
            if tracked.tp2_hit_at and tracked.tp2_price is not None:
                max_profit_pct = max(
                    max_profit_pct,
                    direction_sign * (tracked.tp2_price - entry_price) / entry_price * 100.0,
                )
            if tracked.close_reason == "stop_loss" and tracked.close_price is not None:
                adverse = direction_sign * (tracked.close_price - entry_price) / entry_price * 100.0
                if adverse < 0.0:
                    max_loss_pct = max(max_loss_pct, abs(adverse))

        outcome: SignalOutcome = create_outcome_from_tracked(
            tracked,
            features,
            max_profit_pct=max_profit_pct,
            max_loss_pct=max_loss_pct,
        )
        payload = outcome.to_dict()
        return {str(key): value for key, value in payload.items()}

    async def reconcile_closed_outcomes(self, *, limit: int = 1000) -> int:
        """Backfill closed active_signals rows that missed signal_outcomes.

        Startup stale-expiry cleanup can close rows before the tracker has a
        chance to emit normal lifecycle events. Those rows must still be visible
        to performance analytics instead of appearing as zero/unverified.
        """
        closed_rows = await self.memory_repo.get_active_signals(
            status="closed",
            include_closed=True,
        )
        if not closed_rows:
            return 0
        MAX_BATCH = 50
        # bounded batch: prevent event loop stall on large backlog
        closed_rows = closed_rows[: min(max(1, int(limit)), MAX_BATCH)]
        existing = {
            str(row.get("tracking_id"))
            for row in await self.memory_repo.get_signal_outcomes(last_days=None)
            if row.get("tracking_id")
        }
        payloads: list[dict[str, Any]] = []
        for row in closed_rows:
            tracking_id = str(row.get("tracking_id") or "")
            if not tracking_id or tracking_id in existing:
                continue
            try:
                tracked = self._tracked_from_payload(row)
                payloads.append(self._build_outcome_payload(tracked))
            except (TypeError, ValueError) as exc:
                LOG.debug("closed outcome reconcile skipped %s: %s", tracking_id, exc)
            if len(payloads) >= max(1, int(limit)):
                break
        if not payloads:
            return 0
        await self.memory_repo.save_signal_outcomes_batch(payloads)
        for payload in payloads:
            self.features_store.pop(str(payload.get("tracking_id") or ""), None)
        await self._persist_features_store_async()
        LOG.info("tracking reconciled missing closed outcomes | count=%d", len(payloads))
        return len(payloads)

    async def reconcile_tp1_outcome_remap(self, *, limit: int = 200) -> int:
        """Re-save outcomes where G1 remap applies but DB still shows expired_active."""
        rows = await self.memory_repo.get_tp1_remap_candidates(limit=limit)
        if not rows:
            return 0
        payloads: list[dict[str, Any]] = []
        for row in rows:
            tracking_id = str(row.get("tracking_id") or "")
            if not tracking_id:
                continue
            try:
                tracked = self._tracked_from_payload(row)
                payloads.append(self._build_outcome_payload(tracked))
            except (TypeError, ValueError) as exc:
                LOG.debug("tp1 outcome remap skipped %s: %s", tracking_id, exc)
        if not payloads:
            return 0
        await self.memory_repo.save_signal_outcomes_batch(payloads)
        LOG.info("tracking remapped tp1 outcomes | count=%d", len(payloads))
        return len(payloads)

    async def _arm_signal(
        self,
        signal: Signal,
        *,
        signal_message_id: int | None,
    ) -> TrackedSignalState:
        created_at = signal.created_at.astimezone(UTC)
        tracked = TrackedSignalState(
            tracking_id=signal.tracking_id,
            tracking_ref=signal.tracking_ref,
            signal_key=signal.signal_key,
            symbol=signal.symbol,
            setup_id=signal.setup_id,
            direction=signal.direction,
            timeframe=signal.timeframe,
            created_at=created_at.isoformat(),
            pending_expires_at=(
                created_at + timedelta(minutes=self.settings.tracking.pending_expiry_minutes)
            ).isoformat(),
            active_expires_at=(
                created_at
                + timedelta(minutes=min(self.settings.tracking.active_expiry_minutes, 240))
            ).isoformat(),
            entry_low=signal.entry_low,
            entry_high=signal.entry_high,
            entry_mid=signal.entry_mid,
            initial_stop=signal.stop,
            stop=signal.stop,
            stop_price=signal.stop,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            take_profit_3=signal.tp3,
            tp1_price=signal.take_profit_1,
            tp2_price=signal.take_profit_2,
            tp3_price=signal.tp3,
            valid_until=signal.valid_until_iso,
            scale_weights=signal.scale_weights,
            ttl_bars=signal.ttl_bars,
            single_target_mode=signal.single_target_mode,
            target_integrity_status=signal.target_integrity_status or "unchecked",
            score=signal.score,
            risk_reward=float(signal.risk_reward or 0.0),
            reasons=signal.reasons,
            signal_message_id=signal_message_id,
            bias_4h=signal.bias_4h,
            quote_volume=signal.quote_volume,
            spread_bps=signal.spread_bps,
            atr_pct=signal.atr_pct,
            orderflow_delta_ratio=signal.orderflow_delta_ratio,
            entry_order_type=str(
                getattr(signal, "entry_order_type", None) or DEFAULT_ENTRY_ORDER_TYPE
            ),
            confirmation_profile=str(
                getattr(signal, "confirmation_profile", None) or "trend_follow"
            ),
            status="pending",
        )
        await self.memory_repo.save_active_signal(self._tracked_to_payload(tracked))
        await self.memory_repo.increment_tracking_stats(signals_sent=1)
        return tracked

    async def _mark_activated(
        self,
        tracked: TrackedSignalState,
        *,
        activated_at: datetime,
        price: float,
        precision_mode: str,  # noqa: ARG002
    ) -> None:
        tracked.status = "active"
        tracked.activated_at = activated_at.astimezone(UTC).isoformat()
        tracked.activation_price = price
        tracked.last_checked_at = activated_at.astimezone(UTC).isoformat()
        tracked.last_price = price
        self._update_price_excursion(tracked, price)
        await self.memory_repo.save_active_signal(self._tracked_to_payload(tracked))
        await self.memory_repo.increment_tracking_stats(activated=1)

    async def _mark_tp1(
        self,
        tracked: TrackedSignalState,
        *,
        occurred_at: datetime,
        price: float,
        precision_mode: str,  # noqa: ARG002
        move_stop_to_break_even: bool,
    ) -> None:
        if tracked.tp1_hit_at is not None:
            return
        tracked.tp1_hit_at = occurred_at.astimezone(UTC).isoformat()
        tracked.tp1_price = price
        if move_stop_to_break_even:
            be_price = tracked.activation_price or tracked.entry_mid
            if be_price is not None and float(be_price) > 0.0:
                self._apply_trailing_stop(tracked, float(be_price))
        await self.memory_repo.save_active_signal(self._tracked_to_payload(tracked))
        await self.memory_repo.increment_tracking_stats(tp1_hit=1)

    async def _mark_checked(
        self,
        tracked: TrackedSignalState,
        *,
        checked_at: datetime,
        last_price: float | None,
        precision_mode: str,  # noqa: ARG002
    ) -> None:
        tracked.last_checked_at = checked_at.astimezone(UTC).isoformat()
        tracked.last_price = last_price
        self._update_price_excursion(tracked, last_price)
        await self.memory_repo.save_active_signal(self._tracked_to_payload(tracked))

    async def _close_signal(
        self,
        tracked: TrackedSignalState,
        *,
        reason: str,
        occurred_at: datetime,
        price: float | None,
        precision_mode: str,  # noqa: ARG002
    ) -> None:
        reason = resolve_terminal_close_reason(tracked, reason)
        if reason == "tp1_hit" and tracked.tp1_hit_at is None:
            tracked.tp1_hit_at = occurred_at.astimezone(UTC).isoformat()
            tracked.tp1_price = tracked.take_profit_1
            if tp1_reached_from_excursion(tracked) and price is None:
                price = tracked.take_profit_1
        tracked.status = "closed"
        tracked.closed_at = occurred_at.astimezone(UTC).isoformat()
        tracked.close_reason = reason
        tracked.close_price = (
            price
            if reason != "tp1_hit"
            else (price if price is not None else tracked.tp1_price or tracked.take_profit_1)
        )
        if reason == "tp1_hit":
            if tracked.tp1_hit_at is None:
                tracked.tp1_hit_at = occurred_at.astimezone(UTC).isoformat()
            tracked.tp1_price = price if price is not None else tracked.take_profit_1
        elif reason == "tp2_hit":
            tracked.tp2_hit_at = occurred_at.astimezone(UTC).isoformat()
            tracked.tp2_price = price
        elif reason in {"stop_loss", "breakeven_stop"} and price is not None:
            tracked.stop_price = price
        await self.memory_repo.save_active_signal(self._tracked_to_payload(tracked))

        deltas = {
            "tp1_hit": {"tp1_hit": 1},
            "tp2_hit": {"tp2_hit": 1},
            "stop_loss": {"stop_loss": 1},
            "breakeven_stop": {"stop_loss": 1},
            "expired": {"expired": 1},
            "ambiguous_exit": {"ambiguous_exit": 1},
            "setup_invalidated": {"setup_invalidated": 1},
        }.get(reason)
        if deltas:
            await self.memory_repo.increment_tracking_stats(**deltas)

    async def _apply_outcome_cooldowns(
        self,
        tracked: TrackedSignalState,
        event_type: str,
        occurred_at: datetime,
    ) -> None:
        filters = self.settings.filters
        repo = self.memory_repo
        sent_at = occurred_at.astimezone(UTC)
        symbol = str(tracked.symbol or "").upper()
        setup_id = str(tracked.setup_id or "")
        direction = str(tracked.direction or "").lower()

        symbol_minutes = int(getattr(filters, "symbol_cooldown_minutes", 0) or 0)
        if symbol_minutes > 0:
            await repo.set_cooldown(
                f"strategy_symbol_direction:{symbol}:{direction}",
                sent_at,
                setup_id,
                symbol,
                "outcome_symbol_direction",
            )

        if event_type not in {"stop_loss", "breakeven_stop"}:
            return

        sl_minutes = int(getattr(filters, "outcome_sl_cooldown_minutes", 0) or 0)
        if sl_minutes <= 0:
            return

        await repo.set_cooldown(
            f"outcome_sl:{setup_id}:{symbol}",
            sent_at,
            setup_id,
            symbol,
            "outcome_sl",
        )
        family = catalog_setup_family(setup_id)
        await repo.set_cooldown(
            f"outcome_sl_family:{family}:{symbol}:{direction}",
            sent_at,
            setup_id,
            symbol,
            "outcome_sl_family",
        )

    async def _record_setup_outcome(
        self,
        setup_id: str,
        event_type: str,
        *,
        pnl_r_multiple: float | None = None,
        was_profitable: bool | None = None,
    ) -> None:
        await self.memory_repo.record_setup_outcome(
            setup_id,
            event_type,
            pnl_r_multiple=pnl_r_multiple,
            was_profitable=was_profitable,
        )

    @staticmethod
    def _tracked_r_multiple(tracked: TrackedSignalState) -> float | None:
        if tracked.activated_at is None:
            return None
        entry_price = tracked.activation_price or tracked.entry_mid
        exit_price = tracked.close_price
        risk_stop = tracked.initial_stop if tracked.initial_stop is not None else tracked.stop
        if not entry_price or not exit_price:
            return None
        try:
            entry = float(entry_price)
            exit_px = float(exit_price)
            stop = float(risk_stop)
        except TypeError, ValueError:
            return None
        if not all(math.isfinite(value) for value in (entry, exit_px, stop)):
            return None
        risk = abs(entry - stop)
        if risk <= 0.0:
            return None
        pnl = exit_px - entry if tracked.direction == "long" else entry - exit_px
        return pnl / risk

    async def repair_stuck_pending_activations(
        self,
        *,
        dry_run: bool = False,
    ) -> list[SignalTrackingEvent]:
        """Promote legacy pending rows that logged zone touch but never activated."""
        if dry_run or not self.settings.tracking.enabled:
            return []
        rows = await self.memory_repo.get_active_signals(status="pending")
        events: list[SignalTrackingEvent] = []
        repaired = 0
        now = datetime.now(UTC)
        for row in rows:
            zone_at = row.get("entry_zone_touched_at")
            if not zone_at or row.get("activated_at"):
                continue
            pending_expires_at = parse_state_dt(row.get("pending_expires_at"))
            if pending_expires_at is not None and now > pending_expires_at:
                continue
            try:
                tracked = self._tracked_from_payload(row)
            except TypeError, ValueError:
                LOG.debug(
                    "skip stuck pending repair | tracking_id=%s parse_failed",
                    row.get("tracking_id"),
                )
                continue
            touched_dt = parse_state_dt(zone_at) or datetime.now(UTC)
            fill_price = tracked.entry_mid or (tracked.entry_low + tracked.entry_high) / 2.0
            last_price = row.get("last_price")
            if last_price is not None:
                try:
                    lp = float(last_price)
                    if _price_in_entry_zone(tracked, lp):
                        fill_price = lp
                except TypeError, ValueError:
                    pass
            await self._mark_activated(
                tracked,
                activated_at=touched_dt,
                price=fill_price,
                precision_mode="repair",
            )
            event = SignalTrackingEvent(
                event_type="activated",
                tracked=tracked,
                occurred_at=touched_dt,
                event_price=fill_price,
                precision_mode="repair",
                note="legacy_zone_touch_repair",
            )
            events.append(event)
            self.telemetry.append_jsonl(
                "tracking_events.jsonl",
                event.to_log_row(stats=await self._stats_snapshot()),
            )
            repaired += 1
        if repaired:
            LOG.info("repaired stuck pending activations | count=%d", repaired)
            await self._persist_tracking_state()
        return events

    async def arm_signals(self, signals: list[Signal], *, dry_run: bool) -> None:
        await self.arm_signals_with_messages(signals, dry_run=dry_run, message_ids={})

    async def cancel_pending_delivery(
        self,
        signal: Signal,
        *,
        reason: str = "delivery_not_sent",
        dry_run: bool = False,
    ) -> None:
        """Close a pending journal row when Telegram delivery did not complete."""
        if dry_run or not self.settings.tracking.enabled:
            return
        active_rows = await self.memory_repo.get_active_signals(symbol=signal.symbol)
        row = next(
            (item for item in active_rows if item.get("tracking_id") == signal.tracking_id),
            None,
        )
        if row is None:
            return
        try:
            tracked = self._tracked_from_payload(row)
        except TypeError, ValueError:
            return
        if tracked.status != "pending" or tracked.signal_message_id is not None:
            return
        now = datetime.now(UTC)
        await self._close_signal(
            tracked,
            reason=reason,
            occurred_at=now,
            price=tracked.entry_mid,
            precision_mode="delivery_cancel",
        )
        await self._persist_tracking_state()

    async def force_close_tracking_ids(
        self,
        tracking_ids: list[str],
        *,
        reason: str,
        occurred_at: datetime,
        note: str | None = None,
    ) -> list[SignalTrackingEvent]:
        if not tracking_ids:
            return []
        tracked_rows = await self._active_signals()
        if not tracked_rows:
            return []

        wanted = {str(item) for item in tracking_ids if str(item)}
        events: list[SignalTrackingEvent] = []
        for tracked in tracked_rows:
            if tracked.tracking_id not in wanted:
                continue
            price = (
                tracked.last_price
                or tracked.close_price
                or tracked.activation_price
                or tracked.entry_mid
            )
            events.append(
                await self._close_event(
                    tracked,
                    event_type=reason,
                    occurred_at=occurred_at,
                    price=price,
                    precision_mode="system",
                    note=note,
                )
            )

        if tracked_rows:
            try:
                await self._persist_tracking_state()
            except OSError:
                LOG.exception("tracking state persist failed for forced close (continuing)")
        stats = await self._stats_snapshot()
        for event in events:
            self.telemetry.append_jsonl("tracking_events.jsonl", event.to_log_row(stats=stats))
        return events

    async def _close_event(
        self,
        tracked: TrackedSignalState,
        *,
        event_type: str,
        occurred_at: datetime,
        price: float | None,
        precision_mode: str,
        note: str | None = None,
    ) -> SignalTrackingEvent:
        await self._close_signal(
            tracked,
            reason=event_type,
            occurred_at=occurred_at,
            price=price,
            precision_mode=precision_mode,
        )

        # Update adaptive scoring window for the closed setup. Pending signals
        # that never touched entry are persisted as monitoring outcomes, but do
        # not belong in trade-performance adaptation.
        setup_outcome = (
            event_type
            if event_type
            in {
                "tp1_hit",
                "tp2_hit",
                "stop_loss",
                "breakeven_stop",
                "ambiguous_exit",
                "emergency_exit",
                "superseded",
            }
            else "ambiguous_exit"
        )
        if tracked.activated_at is not None and setup_outcome != "superseded":
            try:
                pnl_r_multiple = self._tracked_r_multiple(tracked)
                await self._record_setup_outcome(
                    tracked.setup_id,
                    setup_outcome,
                    pnl_r_multiple=pnl_r_multiple,
                    was_profitable=(pnl_r_multiple > 0.0) if pnl_r_multiple is not None else None,
                )
                if self.quality_monitor is not None and pnl_r_multiple is not None:
                    try:
                        self.quality_monitor.update(
                            tracked.tracking_id,
                            tracked.setup_id,
                            setup_outcome,
                            pnl_r_multiple,
                            symbol=tracked.symbol,
                        )
                    except DEFENSIVE_EXC as exc:
                        LOG.warning("quality_monitor_update_failed", extra={"exc": str(exc)})
            except OSError, ValueError:
                LOG.debug("record_outcome failed for %s (non-critical)", tracked.setup_id)

        # Persist outcome before returning the close event. Fire-and-forget lost
        # low-volume batches when the process exited before the background task
        # had appended/flushed the row.
        try:
            await self._queue_outcome_for_batch(tracked, event_type)
            await self._flush_pending_outcomes()
        except DEFENSIVE_EXC as exc:
            LOG.debug(
                "save_signal_outcome failed for %s/%s: %s",
                tracked.setup_id,
                tracked.tracking_id,
                exc,
            )

        if event_type in {"stop_loss", "breakeven_stop"} and tracked.activated_at is not None:
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(self._capture_post_sl_recovery(tracked))

        if tracked.activated_at is not None and event_type in {
            "stop_loss",
            "breakeven_stop",
            "tp1_hit",
            "tp2_hit",
        }:
            try:
                await self._apply_outcome_cooldowns(tracked, event_type, occurred_at)
            except DEFENSIVE_EXC:
                LOG.debug(
                    "outcome cooldown skipped | tracking_id=%s setup=%s",
                    tracked.tracking_id,
                    tracked.setup_id,
                    exc_info=True,
                )

        return SignalTrackingEvent(
            event_type=event_type,
            tracked=tracked,
            occurred_at=occurred_at,
            event_price=price,
            precision_mode=precision_mode,
            note=note,
        )

    def _save_outcome(self, tracked: TrackedSignalState, _event_type: str) -> None:
        """Сохраняет outcome завершенного сигнала."""
        outcome = self._build_outcome_payload(tracked)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            LOG.debug(
                "save_signal_outcome skipped: no running loop | tracking_id=%s",
                tracked.tracking_id,
            )
        else:
            task = loop.create_task(self.memory_repo.save_signal_outcome(outcome))

            def _log_outcome_task(done: asyncio.Task[object]) -> None:
                # fix-20260604: surface async outcome write failures (was debug-only)
                if done.cancelled():
                    return
                exc = done.exception()
                if exc is not None:
                    LOG.error(
                        "save_signal_outcome failed | tracking_id=%s",
                        tracked.tracking_id,
                        exc_info=exc,
                    )

            task.add_done_callback(_log_outcome_task)

        # Очищаем features store
        self.features_store.pop(tracked.tracking_id, None)
        self._persist_features_store()

    async def _queue_outcome_for_batch(self, tracked: TrackedSignalState, _event_type: str) -> None:
        """Queue outcome for batched I/O."""
        outcome = self._build_outcome_payload(tracked)

        async with self._pending_outcomes_lock:
            self._pending_outcomes.append(outcome)
            should_flush = len(self._pending_outcomes) >= self._pending_outcomes_flush_size

        if should_flush:
            await self._flush_pending_outcomes()

        # Clean up features store immediately (cheap operation)
        self.features_store.pop(tracked.tracking_id, None)
        await self._persist_features_store_async()

    async def supersede_open_signal(
        self,
        new_signal: Signal,
        *,
        dry_run: bool = False,
    ) -> list[SignalTrackingEvent] | None:
        """Close existing open signal when a new better one supersedes it.

        Args:
            new_signal: The new signal that supersedes the old one
            dry_run: If True, don't actually modify state

        Returns:
            List of tracking events if a signal was closed, None otherwise
        """
        if dry_run or not self.settings.tracking.enabled:
            return None

        async with self._symbol_review_lock(new_signal.symbol):
            # Find existing open signals for this symbol after entering the
            # per-symbol lifecycle lock so review/expiry cannot close the same
            # pending row concurrently.
            existing_signals = [
                r
                for r in await self._active_signals(symbol=new_signal.symbol)
                if r.activated_at is None and r.setup_id == new_signal.setup_id
            ]

            if not existing_signals:
                return None

            events: list[SignalTrackingEvent] = []
            now = datetime.now(UTC)

            for existing in existing_signals:
                # Close the existing signal as superseded
                event = await self._close_event(
                    existing,
                    event_type="superseded",
                    occurred_at=now,
                    price=None,
                    precision_mode="theoretical",
                    note=f"Superseded by {new_signal.setup_id} (score: {new_signal.score:.2f})",
                )
                events.append(event)

            if events:
                try:
                    await self._persist_tracking_state()
                except OSError:
                    LOG.exception("tracking state persist failed for supersede (continuing)")

                for event in events:
                    self.telemetry.append_jsonl(
                        "tracking_events.jsonl",
                        event.to_log_row(stats=await self._stats_snapshot()),
                    )

            return events or None

    # ------------------------------------------------------------------
    # Event-driven additions (Фаза 1 рефакторинга)
    # ------------------------------------------------------------------

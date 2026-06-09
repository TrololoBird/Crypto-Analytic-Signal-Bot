"""TP/SL and market-data review logic for SignalTracker (Phase G)."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from bisect import bisect_right
from datetime import UTC, datetime
from typing import Any

import polars as pl

from bot.domain.limit_entry import normalize_confirmation_profile
from bot.persistence.activation_gate import evaluate_pre_activation
from bot.persistence.tracking_events import SignalTrackingEvent
from bot.runtime.errors import DEFENSIVE_EXC

from ..domain.limit_entry import (
    pending_expiry_minutes_for_signal,
    should_activate_limit_entry,
    should_activate_limit_fill_price,
)
from ..domain.schemas import AggTrade
from ..market.data import MarketDataUnavailable
from ..persistence.sl_diagnostics import classify_stop_loss_root_cause
from ..persistence.tracked import (
    TrackedSignalState,
    parse_state_dt,
    resolve_terminal_close_reason,
)

LOG = logging.getLogger("bot.tracking")

_REVERSAL_PROFILES = frozenset({"countertrend_exhaustion", "divergence_reversal"})
_HARD_GATE_CLOSE_PREFIXES = (
    "activation_staleness",
    "pending_too_old",
    "activation_context_stale",
    "activation_trend_regime_",
    "activation_score_decay",
)
_HARD_GATE_CLOSE_NOTES = frozenset(
    {
        "zone_invalidated_stop_breached",
        "activation_blocked_supertrend_up_short",
        "activation_blocked_supertrend_down_long",
    }
)


def _gate_note_closes_pending(note: str) -> bool:
    if note in _HARD_GATE_CLOSE_NOTES:
        return True
    return any(note.startswith(prefix) for prefix in _HARD_GATE_CLOSE_PREFIXES)


def _expiry_event_type(tracked: TrackedSignalState) -> str:
    """Map expiry to tp1_hit when TP1 was reached before terminal close (G1)."""
    return resolve_terminal_close_reason(tracked, "expired")


def _stop_close_event_type(tracked: TrackedSignalState) -> str:
    """Classify stop hit as loss vs break-even after TP1 stop migration."""
    if tracked.tp1_hit_at is None:
        return "stop_loss"
    be_price = tracked.activation_price or tracked.entry_mid
    if be_price is None or float(be_price) <= 0.0:
        return "stop_loss"
    tol = max(abs(float(be_price)) * 1e-4, 1e-8)
    if abs(float(tracked.stop) - float(be_price)) <= tol:
        return "breakeven_stop"
    return "stop_loss"


class TPSLReviewMixin:
    """Applies aggTrade/candle/tick review to open tracked signals."""

    _last_agg_trade_fetch_mono: float
    _agg_trade_semaphore: asyncio.Semaphore

    def _preferred_review_price(
        self,
        symbol: str,
        fallback_price: float,
        *,
        activated: bool,
    ) -> tuple[float, str]:
        """Use fresh mark price for post-activation SL/TP (Binance perp trigger convention)."""
        if not activated:
            return fallback_price, "trade"
        ws = getattr(self.market_data, "_ws", None)
        if ws is None:
            return fallback_price, "trade"
        snapshot_fn = getattr(ws, "get_mark_price_snapshot", None)
        age_fn = getattr(ws, "get_mark_price_age_seconds", None)
        if snapshot_fn is None or age_fn is None:
            return fallback_price, "trade"
        snapshot = snapshot_fn(symbol)
        if not snapshot:
            return fallback_price, "trade"
        mark_price = float(snapshot.get("mark_price") or 0.0)
        if mark_price <= 0.0:
            return fallback_price, "trade"
        age = age_fn(symbol)
        max_age = float(getattr(self.settings.tracking, "mark_price_max_age_seconds", 30.0))
        if age is None or float(age) > max_age:
            return fallback_price, "trade"
        return mark_price, "mark"

    def _effective_stop(self, tracked: TrackedSignalState) -> float:
        trail = getattr(tracked, "trailing_stop", None)
        if trail is not None and float(trail) > 0.0:
            return float(trail)
        cached = getattr(self, "_trailing_stops", {}).get(tracked.tracking_id)
        if cached is not None and float(cached) > 0.0:
            return float(cached)
        return float(tracked.stop)

    def _activation_feature_payload(self, tracked: TrackedSignalState) -> dict[str, Any] | None:
        feat = self.features_store.get(tracked.tracking_id)
        if feat is None:
            return None
        return feat.to_dict()

    def _pre_activation_allowed(
        self,
        tracked: TrackedSignalState,
        *,
        price: float,
        now: datetime,
        bar_open: float | None = None,
        bar_close: float | None = None,
        bar_high: float | None = None,
        bar_low: float | None = None,
    ) -> tuple[bool, str]:
        tracking_cfg = self.settings.tracking
        if not getattr(tracking_cfg, "pre_activation_gate_enabled", True):
            return True, "gate_disabled"
        profile = normalize_confirmation_profile(tracked.confirmation_profile)
        pending_minutes = pending_expiry_minutes_for_signal(
            self.settings,
            confirmation_profile=profile,
            entry_order_type=tracked.entry_order_type,
            setup_id=tracked.setup_id,
        )
        ok, note = evaluate_pre_activation(
            tracked,
            price=price,
            now=now,
            features=self._activation_feature_payload(tracked),
            bar_open=bar_open,
            bar_close=bar_close,
            bar_high=bar_high,
            bar_low=bar_low,
            staleness_atr_mult=float(
                getattr(tracking_cfg, "activation_staleness_atr_mult", 1.2)
            ),
            max_pending_minutes=pending_minutes,
            min_score_at_activation=float(getattr(tracking_cfg, "activation_min_score", 0.65)),
            score_decay_per_15m_bar=float(
                getattr(tracking_cfg, "activation_score_decay_per_bar", 0.03)
            ),
            context_max_age_seconds=float(
                getattr(tracking_cfg, "activation_context_max_age_seconds", 120.0)
            ),
            reversal_activation_pin_required=bool(
                getattr(tracking_cfg, "reversal_activation_pin_required", False)
            ),
        )
        if not ok:
            tracked.last_lifecycle_note = note
            LOG.info(
                "pre_activation_blocked | ref=%s setup=%s symbol=%s note=%s",
                tracked.tracking_ref,
                tracked.setup_id,
                tracked.symbol,
                note,
            )
        return ok, note

    @staticmethod
    def _update_price_excursion(tracked: TrackedSignalState, price: float | None) -> None:
        if price is None or price <= 0.0:
            return
        entry = tracked.activation_price or tracked.entry_mid
        if not entry or entry <= 0.0:
            return
        direction_sign = 1.0 if tracked.direction == "long" else -1.0
        move_pct = direction_sign * (float(price) - float(entry)) / float(entry) * 100.0
        tracked.max_favorable_pct = max(tracked.max_favorable_pct, move_pct)
        if move_pct < 0.0 and abs(move_pct) > tracked.max_adverse_pct:
            tracked.max_adverse_pct = abs(move_pct)

    @staticmethod
    def _update_bar_excursion(
        tracked: TrackedSignalState,
        *,
        high: float,
        low: float,
    ) -> None:
        """Update MAE/MFE using full bar extremes while a signal is active."""
        if tracked.activated_at is None:
            return
        if tracked.direction == "long":
            TPSLReviewMixin._update_price_excursion(tracked, high)
            entry = tracked.activation_price or tracked.entry_mid
            if entry and entry > 0.0 and low < entry:
                adverse = (float(entry) - float(low)) / float(entry) * 100.0
                tracked.max_adverse_pct = max(tracked.max_adverse_pct, adverse)
            return
        TPSLReviewMixin._update_price_excursion(tracked, low)
        entry = tracked.activation_price or tracked.entry_mid
        if entry and entry > 0.0 and high > entry:
            adverse = (float(high) - float(entry)) / float(entry) * 100.0
            tracked.max_adverse_pct = max(tracked.max_adverse_pct, adverse)

    async def _capture_post_sl_recovery(self, tracked: TrackedSignalState) -> None:
        """Record 6h post-stop favorable move into outcome features."""
        sl_close = tracked.close_reason in {"stop_loss", "breakeven_stop"}
        if not sl_close or tracked.activated_at is None:
            return
        closed_at = parse_state_dt(tracked.closed_at)
        if closed_at is None:
            return
        exit_price = tracked.close_price or tracked.stop
        if exit_price is None or float(exit_price) <= 0.0:
            return
        try:
            candles = await self.market_data.fetch_klines(tracked.symbol, "15m", limit=24)
        except DEFENSIVE_EXC:
            return
        if candles.is_empty():
            return
        direction_sign = 1.0 if tracked.direction == "long" else -1.0
        exit_px = float(exit_price)
        tp1 = float(tracked.take_profit_1)
        max_favorable = 0.0
        for row in candles.select(["close_time", "high", "low"]).to_dicts():
            bar_time = row["close_time"]
            if isinstance(bar_time, str):
                bar_time = datetime.fromisoformat(bar_time)
            if bar_time <= closed_at:
                continue
            if (bar_time - closed_at).total_seconds() > 6 * 3600:
                break
            bar_high = float(row["high"])
            bar_low = float(row["low"])
            probe = bar_high if tracked.direction == "long" else bar_low
            move = direction_sign * (probe - exit_px) / exit_px * 100.0
            if move > max_favorable:
                max_favorable = move
        tp1_room = direction_sign * (tp1 - exit_px) / exit_px * 100.0 if tp1 > 0.0 else 0.0
        regime_at_close: str | None = None
        try:
            market_ctx = await self.memory_repo.get_market_context()
            if isinstance(market_ctx, dict):
                raw_regime = market_ctx.get("market_regime")
                if raw_regime is not None:
                    regime_at_close = str(raw_regime)
        except DEFENSIVE_EXC:
            regime_at_close = None
        post_sl_features: dict[str, Any] = {
            "post_sl_favorable_pct": round(max_favorable, 4),
            "post_sl_tp1_room_pct": round(tp1_room, 4),
            "post_sl_window_hours": 6,
        }
        if regime_at_close:
            post_sl_features["market_regime_at_close"] = regime_at_close
        await self.memory_repo.merge_outcome_features(tracked.tracking_id, post_sl_features)
        features = self.features_store.get(tracked.tracking_id)
        feat_dict = features.to_dict() if features else {}
        feat_dict.update(post_sl_features)
        created_at = parse_state_dt(tracked.created_at) or closed_at
        activated_at = parse_state_dt(tracked.activated_at)
        time_to_entry_min = (
            int((activated_at - created_at).total_seconds() / 60) if activated_at else 0
        )
        time_to_exit_min = int((closed_at - created_at).total_seconds() / 60)
        sl_diag = classify_stop_loss_root_cause(
            direction=tracked.direction,
            mfe=float(tracked.max_favorable_pct or 0.0),
            mae=float(tracked.max_adverse_pct or 0.0),
            time_to_entry_min=time_to_entry_min,
            time_to_exit_min=time_to_exit_min,
            features=feat_dict,
        )
        await self.memory_repo.merge_outcome_features(
            tracked.tracking_id,
            {
                "sl_root_cause": sl_diag["code"],
                "sl_root_cause_label": sl_diag["label"],
                "sl_diagnostics": sl_diag,
            },
        )

    async def review_open_signals(self, *, dry_run: bool) -> list[SignalTrackingEvent]:
        if dry_run or not self.settings.tracking.enabled:
            return []
        tracked_rows = await self._active_signals()
        self._cleanup_symbol_review_locks({row.symbol for row in tracked_rows})
        if not tracked_rows:
            return []

        now = datetime.now(UTC)
        events: list[SignalTrackingEvent] = []
        review_rows: list[TrackedSignalState] = []
        for tracked in tracked_rows:
            pending_expires_at = parse_state_dt(tracked.pending_expires_at)
            if (
                tracked.activated_at is None
                and pending_expires_at is not None
                and now > pending_expires_at
            ):
                events.extend(
                    await self._apply_time_fallback(
                        tracked,
                        now=now,
                        precision_mode="time_fallback",
                        note="pending_expired_channel_notify",
                    )
                )
            else:
                review_rows.append(tracked)

        if events:
            try:
                await self._persist_tracking_state()
            except OSError:
                LOG.exception(
                    "tracking state persist failed for expired pending fast-path (continuing)"
                )
            stats = await self._stats_snapshot()
            for event in events:
                self.telemetry.append_jsonl(
                    "tracking_events.jsonl",
                    event.to_log_row(stats=stats),
                )

        if not review_rows:
            return events

        by_symbol: dict[str, list[TrackedSignalState]] = {}
        for tracked in review_rows:
            by_symbol.setdefault(tracked.symbol, []).append(tracked)

        symbols = sorted(by_symbol.keys())
        for idx, symbol in enumerate(symbols):
            if idx > 0:
                await asyncio.sleep(self._agg_trade_startup_gap_s)
            events.extend(
                await self._review_symbol_open_signals_locked(
                    symbol,
                    now=now,
                    fallback_precision_mode="time_fallback",
                    persist_error_context=(
                        "tracking state persist failed (continuing without persistence)"
                    ),
                )
            )
        return events

    async def _review_symbol(
        self,
        symbol: str,
        tracked_rows: list[TrackedSignalState],
        *,
        now: datetime,
    ) -> list[SignalTrackingEvent]:
        oldest_check = now
        for tracked in tracked_rows:
            last_checked = (
                parse_state_dt(tracked.last_checked_at) or parse_state_dt(tracked.created_at) or now
            )
            oldest_check = min(oldest_check, last_checked)
        start_time_ms = int(oldest_check.timestamp() * 1000)
        end_time_ms = int(now.timestamp() * 1000)

        try:
            async with self._agg_trade_semaphore:
                gap = self._agg_trade_min_gap_s - (
                    time.monotonic() - self._last_agg_trade_fetch_mono
                )
                if gap > 0.0:
                    await asyncio.sleep(gap)
                self._last_agg_trade_fetch_mono = time.monotonic()
                trades, complete = await asyncio.wait_for(
                    self.market_data.fetch_agg_trades(
                        symbol,
                        start_time_ms=start_time_ms,
                        end_time_ms=end_time_ms,
                        page_limit=self.settings.tracking.agg_trade_page_limit,
                        page_size=self.settings.tracking.agg_trade_page_size,
                    ),
                    timeout=self.settings.ws.rest_timeout_seconds,
                )
        except TimeoutError:
            LOG.debug("agg trades timed out for %s; falling back to candles", symbol)
            trades = []
            complete = False
        except MarketDataUnavailable as exc:
            LOG.debug(
                "agg trades unavailable for %s; falling back to candles: %s",
                symbol,
                exc,
            )
            trades = []
            complete = False
        except (ValueError, TypeError) as exc:
            LOG.debug(
                "agg trades response invalid for %s; falling back to candles: %s", symbol, exc
            )
            trades = []
            complete = False
        except DEFENSIVE_EXC:
            LOG.exception("agg trades review failed for %s; falling back to candles", symbol)
            trades = []
            complete = False

        if complete:
            events: list[SignalTrackingEvent] = []
            trade_times = [trade.trade_time for trade in trades]
            for tracked in tracked_rows:
                events.extend(
                    await self._apply_trade_rows(tracked, trades, trade_times=trade_times, now=now)
                )
            return events

        lookback_minutes = max(30, int((now - oldest_check).total_seconds() / 60.0) + 5)
        lookback_limit = min(max(lookback_minutes, 60), 1000)
        try:
            candles = await self.market_data.fetch_klines(symbol, "1m", limit=lookback_limit)
        except MarketDataUnavailable as exc:
            LOG.info(
                "tracking candles unavailable for %s; using time fallback: %s",
                symbol,
                exc,
            )
            return await self._apply_time_fallback_rows(
                tracked_rows,
                now=now,
                precision_mode="time_fallback",
            )
        except DEFENSIVE_EXC:
            LOG.exception("tracking candle review failed for %s; using time fallback", symbol)
            return await self._apply_time_fallback_rows(
                tracked_rows,
                now=now,
                precision_mode="time_fallback",
            )
        events = []
        if self._check_adverse_event(candles):
            events.extend(await self._invalidate_all_for_adverse_event(tracked_rows, now=now))
            return events
        for tracked in tracked_rows:
            try:
                events.extend(await self._apply_candle_rows(tracked, candles, now=now))
            except DEFENSIVE_EXC:
                LOG.exception(
                    "tracking candle application failed for %s/%s; using time fallback",
                    symbol,
                    tracked.tracking_id,
                )
                events.extend(
                    await self._apply_time_fallback(
                        tracked,
                        now=now,
                        precision_mode="time_fallback",
                    )
                )
        return events

    async def _apply_trade_rows(
        self,
        tracked: TrackedSignalState,
        trades: list[AggTrade],
        *,
        trade_times: list[datetime] | None = None,
        now: datetime,
    ) -> list[SignalTrackingEvent]:
        events: list[SignalTrackingEvent] = []
        last_checked = (
            parse_state_dt(tracked.last_checked_at) or parse_state_dt(tracked.created_at) or now
        )
        pending_expires_at = parse_state_dt(tracked.pending_expires_at) or now
        parse_state_dt(tracked.active_expires_at) or now
        last_price = tracked.last_price

        relevant = trades
        if trades:
            ordered_times = trade_times or [trade.trade_time for trade in trades]
            start_idx = bisect_right(ordered_times, last_checked)
            relevant = trades[start_idx:]
        for trade in relevant:
            last_price = trade.price
            if tracked.activated_at is None:
                if trade.trade_time > pending_expires_at:
                    events.append(
                        await self._close_event(
                            tracked,
                            event_type=_expiry_event_type(tracked),
                            occurred_at=pending_expires_at,
                            price=last_price,
                            precision_mode="trade",
                        )
                    )
                    return events
                # Market orders enter at current price immediately (no zone re-touch
                # wait — that is limit semantics). Limit orders fill only when price
                # trades back into the published zone.
                if str(tracked.entry_order_type or "limit").strip().lower() == "market":
                    fill_ok, fill_note = True, "market_immediate_fill"
                else:
                    fill_ok, fill_note = should_activate_limit_fill_price(
                        entry_low=tracked.entry_low,
                        entry_high=tracked.entry_high,
                        price=trade.price,
                    )
                if fill_ok:
                    profile = normalize_confirmation_profile(tracked.confirmation_profile)
                    if profile in _REVERSAL_PROFILES:
                        if tracked.entry_zone_touched_at is None:
                            tracked.entry_zone_touched_at = (
                                trade.trade_time.astimezone(UTC).isoformat()
                            )
                            tracked.entry_confirm_pending_at = (
                                trade.trade_time.astimezone(UTC).isoformat()
                            )
                        await self._mark_checked(
                            tracked,
                            checked_at=trade.trade_time,
                            last_price=last_price,
                            precision_mode="trade",
                        )
                        continue
                    gate_ok, gate_note = self._pre_activation_allowed(
                        tracked,
                        price=trade.price,
                        now=now,
                    )
                    if not gate_ok:
                        if _gate_note_closes_pending(gate_note):
                            events.append(
                                await self._close_event(
                                    tracked,
                                    event_type="unactivated_close",
                                    occurred_at=trade.trade_time,
                                    price=trade.price,
                                    precision_mode="trade",
                                    note=gate_note,
                                )
                            )
                            return events
                        if tracked.entry_zone_touched_at is None:
                            tracked.entry_zone_touched_at = (
                                trade.trade_time.astimezone(UTC).isoformat()
                            )
                        await self._mark_checked(
                            tracked,
                            checked_at=trade.trade_time,
                            last_price=last_price,
                            precision_mode="trade",
                        )
                        continue
                    if tracked.entry_zone_touched_at is None:
                        tracked.entry_zone_touched_at = trade.trade_time.astimezone(UTC).isoformat()
                    await self._mark_activated(
                        tracked,
                        activated_at=trade.trade_time,
                        price=trade.price,
                        precision_mode="trade",
                    )
                    events.append(
                        SignalTrackingEvent(
                            event_type="activated",
                            tracked=tracked,
                            occurred_at=trade.trade_time,
                            event_price=trade.price,
                            precision_mode="trade_realtime",
                            note=fill_note,
                        )
                    )
                else:
                    await self._mark_checked(
                        tracked,
                        checked_at=trade.trade_time,
                        last_price=last_price,
                        precision_mode="trade",
                    )
                    continue
            review_price, price_mode = self._preferred_review_price(
                tracked.symbol,
                trade.price,
                activated=tracked.activated_at is not None,
            )
            tick_events, closed = await self._apply_price_tick(
                tracked,
                price=review_price,
                occurred_at=trade.trade_time,
                precision_mode=f"trade_{price_mode}",
            )
            if tick_events:
                events.extend(tick_events)
            if closed:
                return events

        if tracked.activated_at is None and now > pending_expires_at:
            events.append(
                await self._close_event(
                    tracked,
                    event_type=_expiry_event_type(tracked),
                    occurred_at=pending_expires_at,
                    price=last_price,
                    precision_mode="trade",
                )
            )
            return events
        await self._mark_checked(
            tracked, checked_at=now, last_price=last_price, precision_mode="trade"
        )
        return events

    async def _invalidate_all_for_adverse_event(
        self,
        tracked_rows: list[TrackedSignalState],
        *,
        now: datetime,
    ) -> list[SignalTrackingEvent]:
        events: list[SignalTrackingEvent] = []
        for tracked in tracked_rows:
            if tracked.activated_at is None:
                continue
            events.append(
                await self._close_event(
                    tracked,
                    event_type="emergency_exit",
                    occurred_at=now,
                    price=float(
                        tracked.last_price or tracked.activation_price or tracked.entry_mid
                    ),
                    precision_mode="adverse_event",
                )
            )
        return events

    @staticmethod
    def _check_adverse_event(candles: pl.DataFrame) -> bool:
        """Detect flash crash: drop >4% within 5 min from recent 1m candles."""
        required = {"close", "close_time"}
        if candles.is_empty() or not required.issubset(candles.columns):
            return False
        recent = candles.tail(5)
        if recent.height < 3:
            return False
        closes = [float(c) for c in recent["close"] if c is not None and math.isfinite(float(c))]
        if len(closes) < 3:
            return False
        first_close = closes[0]
        last_close = closes[-1]
        if first_close <= 0.0:
            return False
        change_pct = (last_close - first_close) / first_close * 100.0
        time_col = list(recent["close_time"])
        if len(time_col) >= 2:
            t0 = time_col[0]
            if not isinstance(t0, datetime):
                t0 = datetime.fromisoformat(str(t0))
            t1 = time_col[-1]
            if not isinstance(t1, datetime):
                t1 = datetime.fromisoformat(str(t1))
            if (t1 - t0).total_seconds() > 600:
                return False
        return change_pct <= -4.0

    async def _update_trailing_post_tp1(
        self,
        tracked: TrackedSignalState,
        *,
        high: float,
        low: float,
    ) -> None:
        """Trail stop by ATR after TP1 hit — 1.5x initial R from best price."""
        if tracked.tp1_hit_at is None:
            return
        entry = tracked.activation_price or tracked.entry_mid
        stop_init = float(tracked.stop)
        if not entry or entry <= 0.0 or stop_init <= 0.0:
            return
        initial_r = abs(float(entry) - stop_init)
        if initial_r <= 0.0:
            return
        if tracked.direction == "long":
            best = high if math.isfinite(high) else 0.0
            if best <= 0.0:
                return
            trail_dist = initial_r * 1.5
            new_stop = best - trail_dist
            entry_px = float(entry)
            if new_stop > entry_px and new_stop > self._effective_stop(tracked):
                self._apply_trailing_stop(tracked, new_stop)
        else:
            best = low if math.isfinite(low) else 0.0
            if best <= 0.0:
                return
            trail_dist = initial_r * 1.5
            new_stop = best + trail_dist
            entry_px = float(entry)
            if new_stop < entry_px and new_stop < self._effective_stop(tracked):
                self._apply_trailing_stop(tracked, new_stop)

    async def _apply_candle_rows(
        self,
        tracked: TrackedSignalState,
        candles: pl.DataFrame,
        *,
        now: datetime,
    ) -> list[SignalTrackingEvent]:
        events: list[SignalTrackingEvent] = []
        last_checked = (
            parse_state_dt(tracked.last_checked_at) or parse_state_dt(tracked.created_at) or now
        )
        pending_expires_at = parse_state_dt(tracked.pending_expires_at) or now
        parse_state_dt(tracked.active_expires_at) or now
        last_price = tracked.last_price

        # Filter candles with close_time > last_checked using Polars
        relevant = candles.filter(pl.col("close_time") > last_checked)
        relevant = relevant.sort("close_time")
        last_processed_at: datetime | None = None
        candle_rows = relevant.select(["close_time", "open", "high", "low", "close"]).to_dicts()
        for row in candle_rows:
            bar_close_time = row["close_time"]
            if isinstance(bar_close_time, str):
                bar_close_time = datetime.fromisoformat(bar_close_time)
            bar_open = float(row["open"])
            bar_high = float(row["high"])
            bar_low = float(row["low"])
            bar_close = float(row["close"])
            last_processed_at = bar_close_time
            last_price = bar_close

            _bar_touches_entry(tracked, high=bar_high, low=bar_low)
            tp1_touched = _bar_hits_tp1(tracked, high=bar_high, low=bar_low)
            tp2_touched = _bar_hits_tp2(tracked, high=bar_high, low=bar_low)
            # Stop triggers immediately on price touch (as in real Binance futures trading)
            stop_touched = _bar_hits_stop(
                tracked,
                high=bar_high,
                low=bar_low,
                stop=self._effective_stop(tracked),
            )

            if tracked.activated_at is None:
                if bar_close_time > pending_expires_at:
                    events.append(
                        await self._close_event(
                            tracked,
                            event_type=_expiry_event_type(tracked),
                            occurred_at=pending_expires_at,
                            price=last_price,
                            precision_mode="candle",
                        )
                    )
                    return events
                is_market = str(tracked.entry_order_type or "limit").strip().lower() == "market"
                if is_market:
                    # Market orders enter at current price on the first observed bar
                    # (≈ publish price) — no zone re-touch wait, which is limit semantics.
                    activate_ok, activate_note = True, "market_immediate_fill"
                else:
                    tracking_cfg = self.settings.tracking
                    require_close = bool(
                        getattr(tracking_cfg, "trend_follow_activation_requires_close", True)
                    )
                    activate_ok, activate_note = should_activate_limit_entry(
                        direction=tracked.direction,
                        confirmation_profile=tracked.confirmation_profile,
                        entry_low=tracked.entry_low,
                        entry_high=tracked.entry_high,
                        open_=bar_open,
                        close=bar_close,
                        high=bar_high,
                        low=bar_low,
                        trend_follow_requires_close=require_close,
                    )
                if activate_ok:
                    if tracked.entry_zone_touched_at is None:
                        tracked.entry_zone_touched_at = bar_close_time.astimezone(UTC).isoformat()
                    gate_ok, gate_note = self._pre_activation_allowed(
                        tracked,
                        price=bar_close,
                        now=now,
                        bar_open=bar_open,
                        bar_close=bar_close,
                        bar_high=bar_high,
                        bar_low=bar_low,
                    )
                    if not gate_ok:
                        if gate_note == "await_bar_confirm":
                            tracked.entry_confirm_pending_at = (
                                bar_close_time.astimezone(UTC).isoformat()
                            )
                            continue
                        if _gate_note_closes_pending(gate_note):
                            events.append(
                                await self._close_event(
                                    tracked,
                                    event_type="unactivated_close",
                                    occurred_at=bar_close_time,
                                    price=bar_close,
                                    precision_mode="candle",
                                    note=gate_note,
                                )
                            )
                            return events
                        continue
                    if is_market:
                        # Honest market fill ≈ publish price = zone midpoint; never the
                        # later bar_close (that would back-date a more favourable fill).
                        fill_price = (tracked.entry_low + tracked.entry_high) / 2.0
                    else:
                        fill_price = (
                            bar_close
                            if _price_in_entry_zone(tracked, bar_close)
                            else (tracked.entry_low + tracked.entry_high) / 2.0
                        )
                    await self._mark_activated(
                        tracked,
                        activated_at=bar_close_time,
                        price=fill_price,
                        precision_mode="candle",
                    )
                    events.append(
                        SignalTrackingEvent(
                            event_type="activated",
                            tracked=tracked,
                            occurred_at=bar_close_time,
                            event_price=fill_price,
                            precision_mode="candle",
                            note=activate_note,
                        )
                    )
                if tracked.activated_at is None:
                    continue
            self._update_bar_excursion(tracked, high=bar_high, low=bar_low)
            await self._update_trailing_post_tp1(tracked, high=bar_high, low=bar_low)
            if (tp2_touched and stop_touched) or (
                tracked.tp1_hit_at is None and tp1_touched and stop_touched
            ):
                events.append(
                    await self._close_event(
                        tracked,
                        event_type="ambiguous_exit",
                        occurred_at=bar_close_time,
                        price=bar_close,
                        precision_mode="candle",
                        note="same_bar_conflict",
                    )
                )
                return events
            if tracked.single_target_mode and (tp1_touched or tp2_touched):
                events.append(
                    await self._close_event(
                        tracked,
                        event_type="tp1_hit",
                        occurred_at=bar_close_time,
                        price=tracked.take_profit_1,
                        precision_mode="candle",
                        note="single_target_close",
                    )
                )
                return events
            if tp2_touched:
                if tracked.tp1_hit_at is None:
                    await self._mark_tp1(
                        tracked,
                        occurred_at=bar_close_time,
                        price=tracked.take_profit_1,
                        precision_mode="candle",
                        move_stop_to_break_even=self.settings.tracking.move_stop_to_break_even_on_tp1,
                    )
                    events.append(
                        SignalTrackingEvent(
                            event_type="tp1_hit",
                            tracked=tracked,
                            occurred_at=bar_close_time,
                            event_price=tracked.take_profit_1,
                            precision_mode="candle",
                            note="tp2_implies_tp1",
                        )
                    )
                events.append(
                    await self._close_event(
                        tracked,
                        event_type="tp2_hit",
                        occurred_at=bar_close_time,
                        price=tracked.take_profit_2,
                        precision_mode="candle",
                    )
                )
                return events
            if stop_touched:
                events.append(
                    await self._close_event(
                        tracked,
                        event_type=_stop_close_event_type(tracked),
                        occurred_at=bar_close_time,
                        price=tracked.stop,
                        precision_mode="candle",
                    )
                )
                return events
            if tp1_touched and tracked.tp1_hit_at is None:
                await self._mark_tp1(
                    tracked,
                    occurred_at=bar_close_time,
                    price=tracked.take_profit_1,
                    precision_mode="candle",
                    move_stop_to_break_even=self.settings.tracking.move_stop_to_break_even_on_tp1,
                )
                events.append(
                    SignalTrackingEvent(
                        event_type="tp1_hit",
                        tracked=tracked,
                        occurred_at=bar_close_time,
                        event_price=tracked.take_profit_1,
                        precision_mode="candle",
                    )
                )

        if tracked.activated_at is None and now > pending_expires_at:
            events.append(
                await self._close_event(
                    tracked,
                    event_type=_expiry_event_type(tracked),
                    occurred_at=pending_expires_at,
                    price=last_price,
                    precision_mode="candle",
                )
            )
            return events
        await self._mark_checked(
            tracked,
            checked_at=last_processed_at if last_processed_at is not None else now,
            last_price=last_price,
            precision_mode="candle",
        )
        return events

    async def _apply_time_fallback(
        self,
        tracked: TrackedSignalState,
        *,
        now: datetime,
        precision_mode: str,
        note: str | None = None,
    ) -> list[SignalTrackingEvent]:
        pending_expires_at = parse_state_dt(tracked.pending_expires_at) or now
        last_price = (
            tracked.last_price
            or tracked.close_price
            or tracked.activation_price
            or tracked.entry_mid
        )

        if tracked.activated_at is None and now > pending_expires_at:
            return [
                await self._close_event(
                    tracked,
                    event_type=_expiry_event_type(tracked),
                    occurred_at=pending_expires_at,
                    price=last_price,
                    precision_mode=precision_mode,
                    note=note or "time_fallback_pending_expiry",
                )
            ]
        await self._mark_checked(
            tracked,
            checked_at=now,
            last_price=last_price,
            precision_mode=precision_mode,
        )
        return []

    async def _apply_time_fallback_rows(
        self,
        tracked_rows: list[TrackedSignalState],
        *,
        now: datetime,
        precision_mode: str,
    ) -> list[SignalTrackingEvent]:
        events: list[SignalTrackingEvent] = []
        for tracked in tracked_rows:
            events.extend(
                await self._apply_time_fallback(
                    tracked,
                    now=now,
                    precision_mode=precision_mode,
                )
            )
        return events

    async def _apply_price_tick(
        self,
        tracked: TrackedSignalState,
        *,
        price: float,
        occurred_at: datetime,
        precision_mode: str,
    ) -> tuple[list[SignalTrackingEvent], bool]:
        events: list[SignalTrackingEvent] = []
        stop_level = self._effective_stop(tracked)
        if tracked.direction == "long":
            if price <= stop_level:
                events.append(
                    await self._close_event(
                        tracked,
                        event_type=_stop_close_event_type(tracked),
                        occurred_at=occurred_at,
                        price=price,
                        precision_mode=precision_mode,
                    )
                )
                return events, True
            if tracked.single_target_mode and price >= tracked.take_profit_1:
                events.append(
                    await self._close_event(
                        tracked,
                        event_type="tp1_hit",
                        occurred_at=occurred_at,
                        price=price,
                        precision_mode=precision_mode,
                        note="single_target_close",
                    )
                )
                return events, True
            if price >= tracked.take_profit_2:
                if tracked.tp1_hit_at is None:
                    await self._mark_tp1(
                        tracked,
                        occurred_at=occurred_at,
                        price=tracked.take_profit_1,
                        precision_mode=precision_mode,
                        move_stop_to_break_even=self.settings.tracking.move_stop_to_break_even_on_tp1,
                    )
                    events.append(
                        SignalTrackingEvent(
                            event_type="tp1_hit",
                            tracked=tracked,
                            occurred_at=occurred_at,
                            event_price=tracked.take_profit_1,
                            precision_mode=precision_mode,
                            note="tp2_implies_tp1",
                        )
                    )
                events.append(
                    await self._close_event(
                        tracked,
                        event_type="tp2_hit",
                        occurred_at=occurred_at,
                        price=price,
                        precision_mode=precision_mode,
                    )
                )
                return events, True
            if tracked.tp1_hit_at is None and price >= tracked.take_profit_1:
                await self._mark_tp1(
                    tracked,
                    occurred_at=occurred_at,
                    price=price,
                    precision_mode=precision_mode,
                    move_stop_to_break_even=self.settings.tracking.move_stop_to_break_even_on_tp1,
                )
                events.append(
                    SignalTrackingEvent(
                        event_type="tp1_hit",
                        tracked=tracked,
                        occurred_at=occurred_at,
                        event_price=price,
                        precision_mode=precision_mode,
                    )
                )
                return events, False
            return [], False

        if price >= stop_level:
            events.append(
                await self._close_event(
                    tracked,
                    event_type=_stop_close_event_type(tracked),
                    occurred_at=occurred_at,
                    price=price,
                    precision_mode=precision_mode,
                )
            )
            return events, True
        if tracked.single_target_mode and price <= tracked.take_profit_1:
            events.append(
                await self._close_event(
                    tracked,
                    event_type="tp1_hit",
                    occurred_at=occurred_at,
                    price=price,
                    precision_mode=precision_mode,
                    note="single_target_close",
                )
            )
            return events, True
        if price <= tracked.take_profit_2:
            if tracked.tp1_hit_at is None:
                await self._mark_tp1(
                    tracked,
                    occurred_at=occurred_at,
                    price=tracked.take_profit_1,
                    precision_mode=precision_mode,
                    move_stop_to_break_even=self.settings.tracking.move_stop_to_break_even_on_tp1,
                )
                events.append(
                    SignalTrackingEvent(
                        event_type="tp1_hit",
                        tracked=tracked,
                        occurred_at=occurred_at,
                        event_price=tracked.take_profit_1,
                        precision_mode=precision_mode,
                        note="tp2_implies_tp1",
                    )
                )
            events.append(
                await self._close_event(
                    tracked,
                    event_type="tp2_hit",
                    occurred_at=occurred_at,
                    price=price,
                    precision_mode=precision_mode,
                )
            )
            return events, True
        if tracked.tp1_hit_at is None and price <= tracked.take_profit_1:
            await self._mark_tp1(
                tracked,
                occurred_at=occurred_at,
                price=price,
                precision_mode=precision_mode,
                move_stop_to_break_even=self.settings.tracking.move_stop_to_break_even_on_tp1,
            )
            events.append(
                SignalTrackingEvent(
                    event_type="tp1_hit",
                    tracked=tracked,
                    occurred_at=occurred_at,
                    event_price=price,
                    precision_mode=precision_mode,
                )
            )
            return events, False
        return [], False

    async def review_open_signals_for_symbol(
        self,
        symbol: str,
        *,
        dry_run: bool,
    ) -> list[SignalTrackingEvent]:
        """Per-symbol tracking review - called on each 15m candle close.

        Extracts the single-symbol branch from review_open_signals() so that
        the event-driven engine can review each symbol independently without
        waiting for the full global cycle.
        """
        if dry_run or not self.settings.tracking.enabled:
            return []
        return await self._review_symbol_open_signals_locked(
            symbol,
            now=datetime.now(UTC),
            fallback_precision_mode="time_fallback",
            persist_error_context=(
                f"tracking state persist failed for {symbol} (continuing without persistence)"
            ),
        )

    async def _review_symbol_open_signals_locked(
        self,
        symbol: str,
        *,
        now: datetime,
        fallback_precision_mode: str,
        persist_error_context: str,
    ) -> list[SignalTrackingEvent]:
        start = time.perf_counter()
        tracked_count = 0
        async with self._symbol_review_lock(symbol):
            tracked_rows = await self._active_signals(symbol=symbol)
            tracked_count = len(tracked_rows)
            if not tracked_rows:
                self._record_symbol_review_duration(
                    symbol,
                    elapsed=time.perf_counter() - start,
                    tracked_count=tracked_count,
                )
                return []
            tracked_rows.sort(key=lambda item: item.created_at)
            try:
                events = await self._review_symbol(symbol, tracked_rows, now=now)
            except DEFENSIVE_EXC:
                LOG.exception("tracking review failed for %s; using time fallback", symbol)
                events = await self._apply_time_fallback_rows(
                    tracked_rows,
                    now=now,
                    precision_mode=fallback_precision_mode,
                )
            try:
                await self._persist_tracking_state()
            except OSError:
                LOG.exception(persist_error_context)
            for event in events:
                self.telemetry.append_jsonl(
                    "tracking_events.jsonl",
                    event.to_log_row(stats=await self._stats_snapshot()),
                )
            self._record_symbol_review_duration(
                symbol,
                elapsed=time.perf_counter() - start,
                tracked_count=tracked_count,
            )
            return events

    async def on_agg_trade(
        self,
        symbol: str,
        price: float,
        trade_dt: datetime,
    ) -> list[SignalTrackingEvent]:
        """Real-time tracking on aggTrade ticks (pending zone touch + active TP/SL)."""
        if not self.settings.tracking.enabled:
            return []
        async with self._symbol_review_lock(symbol):
            tracked_rows = await self._active_signals(symbol=symbol)
            if not tracked_rows:
                return []

            trade = AggTrade(
                symbol=symbol.upper(),
                trade_id=0,
                price=price,
                quantity=0.0,
                trade_time_ms=int(trade_dt.timestamp() * 1000),
                is_buyer_maker=False,
            )
            events: list[SignalTrackingEvent] = []
            for tracked in tracked_rows:
                row_events = await self._apply_trade_rows(
                    tracked,
                    [trade],
                    now=trade_dt,
                )
                events.extend(row_events)
                if tracked.status == "closed":
                    break

            if events:
                try:
                    await self._persist_tracking_state()
                except OSError:
                    LOG.exception(
                        (
                            "tracking state persist failed for realtime trade %s "
                            "(continuing without persistence)"
                        ),
                        symbol,
                    )
                for event in events:
                    self.telemetry.append_jsonl(
                        "tracking_events.jsonl",
                        event.to_log_row(stats=await self._stats_snapshot()),
                    )
            return events


def _price_in_entry_zone(tracked: TrackedSignalState, price: float) -> bool:
    return tracked.entry_low <= price <= tracked.entry_high


def _bar_touches_entry(tracked: TrackedSignalState, *, high: float, low: float) -> bool:
    return low <= tracked.entry_high and high >= tracked.entry_low


def _bar_hits_tp1(tracked: TrackedSignalState, *, high: float, low: float) -> bool:
    if tracked.direction == "long":
        return high >= tracked.take_profit_1
    return low <= tracked.take_profit_1


def _bar_hits_tp2(tracked: TrackedSignalState, *, high: float, low: float) -> bool:
    if tracked.direction == "long":
        return high >= tracked.take_profit_2
    return low <= tracked.take_profit_2


def _bar_hits_stop(
    tracked: TrackedSignalState,
    *,
    high: float,
    low: float,
    stop: float | None = None,
) -> bool:
    """Stop hit detection (immediate trigger price hit).

    In real Binance futures trading, stop-limit orders trigger when
    price reaches the stop price, not waiting for candle close.
    """
    stop_level = float(stop if stop is not None else tracked.stop)
    if tracked.direction == "long":
        return low <= stop_level
    return high >= stop_level

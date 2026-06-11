from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from engine.contract import validate_signal_contract
from engine.errors import DEFENSIVE_EXC
from engine.telegram import DeliveryResult

from .formatting import (
    format_analytics_companion_message,
    format_safe_signal_fallback,
    format_signal_message,
    format_tracked_signal_message,
    format_tracking_event_message,
    validate_telegram_html,
)

if TYPE_CHECKING:
    from engine.domain.schemas import Signal

    from ..persistence.tracking import SignalTrackingEvent

LOG = logging.getLogger("bot.delivery.deliver")
_AUDIT_BATCH_LABELS = {"RAW", "CANDIDATE"}
_AUDIT_BATCH_INTERVAL_SECONDS = 5.0
_AUDIT_BATCH_MAX_LINES = 20
_AUDIT_BATCH_MAX_CHARS = 3500


class SignalBroadcaster(Protocol):
    async def preflight_check(self) -> None: ...
    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> DeliveryResult: ...
    async def edit_html(self, message_id: int, text: str) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DeliveredSignal:
    signal: Signal
    status: str = "sent"
    message_id: int | None = None
    reason: str | None = None


def _direction_label(direction: str) -> str:
    return "LONG" if direction == "long" else "SHORT"


def _fmt_audit_metric(name: str, value: float | None, suffix: str = "") -> str | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except TypeError, ValueError:
        return None
    return f"{name}={numeric:.2f}{suffix}"


def _format_signal_audit_text(label: str, signal: Signal, *, final: bool = False) -> str:
    normalized_label = str(label or "").strip().upper()
    parts = [
        f"[{normalized_label}]",
        f"#{signal.tracking_ref}",
        signal.symbol,
        signal.setup_id,
        signal.direction,
        f"{float(signal.score):.2f}",
    ]
    if normalized_label == "CANDIDATE":
        metrics = [
            _fmt_audit_metric("ADX", signal.adx_1h),
            _fmt_audit_metric("ATR", signal.atr_pct, "%"),
        ]
        parts.extend(metric for metric in metrics if metric)
        if signal.passed_filters:
            parts.append("filters_passed=" + ",".join(signal.passed_filters))
    if final:
        parts.append("FINAL")
    parts.append(signal.created_at.astimezone(UTC).strftime("%H:%M:%S"))
    return " ".join(parts)


def _tradingview_interval(timeframe: str) -> str:
    raw = str(timeframe or "").strip().lower()
    mapping = {
        "1m": "1",
        "3m": "3",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "45m": "45",
        "1h": "60",
        "2h": "120",
        "4h": "240",
        "1d": "1D",
    }
    if raw in mapping:
        return mapping[raw]

    for token in ("15m", "1h", "5m", "30m", "4h"):
        if token in raw:
            return mapping[token]
    return "15"


def tradingview_chart_url(symbol: str, timeframe: str) -> str:
    clean_symbol = "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())
    tv_symbol = f"BINANCE:{clean_symbol}.P"
    interval = _tradingview_interval(timeframe)
    return f"https://www.tradingview.com/chart/?symbol={tv_symbol}&interval={interval}"


def format_signal_text(
    signal: Signal,
    *,
    pending_expiry_minutes: int,
    btc_bias: str | None = None,
    tier: str | None = None,
) -> str:
    try:
        return format_signal_message(
            signal,
            pending_expiry_minutes=pending_expiry_minutes,
            btc_bias=btc_bias,
            tier=tier,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        LOG.warning("signal_format_fallback", extra={"exc": str(exc)})
        symbol = html.escape(str(getattr(signal, "symbol", "UNKNOWN") or "UNKNOWN"))
        setup_id = html.escape(str(getattr(signal, "setup_id", "unknown") or "unknown"))
        direction = _direction_label(str(getattr(signal, "direction", "long") or "long"))
        score = float(getattr(signal, "score", 0.0) or 0.0)
        tp2 = getattr(signal, "tp2", None) or getattr(signal, "take_profit_2", "") or ""
        tp3 = getattr(signal, "tp3", None) or getattr(signal, "take_profit_3", "") or ""
        targets = [
            str(value) for value in (getattr(signal, "take_profit_1", ""), tp2, tp3) if value
        ]
        return "\n".join(
            [
                f"<b>LIMIT {direction} {symbol}</b>",
                (
                    f"<b>Setup</b> <code>{setup_id}</code> | "
                    f"<b>Score</b> <code>{score * 100:.0f}%</code>"
                ),
                f"<b>TP</b> <code>{html.escape(' / '.join(targets) or 'n/a')}</code>",
                "<b>Status</b> pending",
            ]
        )


def format_analytics_companion(
    signal: Signal, *, btc_bias: str | None = None, eth_bias: str | None = None
) -> str:
    return format_analytics_companion_message(
        signal,
        btc_bias=btc_bias,
        eth_bias=eth_bias,
    )


def format_tracked_signal_text(tracked: SignalTrackingEvent | object) -> str:
    return format_tracked_signal_message(tracked)


def format_tracking_event_text(event: SignalTrackingEvent) -> str:
    """Subscriber-facing tracking follow-up for the signal channel."""
    return format_tracking_event_message(event)


class SignalDelivery:
    def __init__(
        self,
        broadcaster: SignalBroadcaster,
        *,
        pending_expiry_minutes: int,
        tracking_reply_freshness_minutes: int = 120,
        public_audit: Any | None = None,
    ) -> None:
        self.broadcaster = broadcaster
        self.pending_expiry_minutes = pending_expiry_minutes
        self.tracking_reply_freshness_minutes = tracking_reply_freshness_minutes
        self._public_audit = public_audit
        self._audit_batch_lock = asyncio.Lock()
        self._audit_batch_lines: list[str] = []
        self._audit_batch_task: asyncio.Task[None] | None = None

    async def preflight_check(self) -> None:
        await self.broadcaster.preflight_check()

    async def close(self) -> None:
        await self.flush_signal_audits()

    async def send_signal_audit(
        self,
        label: str,
        signal: Signal,
        *,
        final: bool = False,
    ) -> DeliveryResult:
        normalized_label = str(label or "").strip().upper()
        text = html.escape(_format_signal_audit_text(normalized_label, signal, final=final))
        if normalized_label in _AUDIT_BATCH_LABELS and not final:
            return await self._queue_signal_audit(text, label=normalized_label, signal=signal)

        await self.flush_signal_audits()
        result = await self.broadcaster.send_html(text)
        if result.status == "sent":
            LOG.info(
                "telegram signal audit sent | label=%s symbol=%s setup=%s message_id=%s",
                label,
                signal.symbol,
                signal.setup_id,
                result.message_id,
            )
        else:
            LOG.debug(
                "signal audit not delivered | label=%s status=%s reason=%s symbol=%s setup=%s",
                label,
                result.status,
                result.reason,
                signal.symbol,
                signal.setup_id,
            )
        return result

    async def _queue_signal_audit(
        self,
        text: str,
        *,
        label: str,
        signal: Signal,
    ) -> DeliveryResult:
        should_flush = False
        async with self._audit_batch_lock:
            self._audit_batch_lines.append(text)
            total_chars = sum(len(line) for line in self._audit_batch_lines)
            should_flush = (
                len(self._audit_batch_lines) >= _AUDIT_BATCH_MAX_LINES
                or total_chars >= _AUDIT_BATCH_MAX_CHARS
            )
            if self._audit_batch_task is None or self._audit_batch_task.done():
                self._audit_batch_task = asyncio.create_task(
                    self._flush_signal_audits_later(),
                    name="telegram_signal_audit_batch_flush",
                )

        LOG.debug(
            "signal audit queued | label=%s symbol=%s setup=%s",
            label,
            signal.symbol,
            signal.setup_id,
        )
        if should_flush:
            await self.flush_signal_audits()
        return DeliveryResult(status="queued", reason="batched_audit")

    async def _flush_signal_audits_later(self) -> None:
        try:
            await asyncio.sleep(_AUDIT_BATCH_INTERVAL_SECONDS)
            await self.flush_signal_audits()
        except asyncio.CancelledError:
            if self._audit_batch_lines:
                await self.flush_signal_audits()
            raise
        except DEFENSIVE_EXC:
            LOG.exception("signal audit batch flush failed")

    async def flush_signal_audits(self) -> None:
        async with self._audit_batch_lock:
            lines = list(self._audit_batch_lines)
            self._audit_batch_lines.clear()
            task = self._audit_batch_task
            self._audit_batch_task = None

        current_task = asyncio.current_task()
        if task is not None and task is not current_task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if not lines:
            return

        for chunk in self._chunk_audit_batch(lines):
            result = await self.broadcaster.send_html(chunk)
            if result.status == "sent":
                LOG.info(
                    "telegram signal audit batch sent | rows=%d message_id=%s",
                    chunk.count("<code>"),
                    result.message_id,
                )
            else:
                LOG.debug(
                    "signal audit batch not delivered | status=%s reason=%s rows=%d",
                    result.status,
                    result.reason,
                    chunk.count("<code>"),
                )

    @staticmethod
    def _chunk_audit_batch(lines: list[str]) -> list[str]:
        header = "<b>Signal audit batch</b>"
        chunks: list[str] = []
        current: list[str] = []
        current_len = len(header)
        for line in lines:
            rendered = f"<code>{line}</code>"
            rendered_len = len(rendered) + 1
            if current and current_len + rendered_len > _AUDIT_BATCH_MAX_CHARS:
                chunks.append(header + "\n" + "\n".join(current))
                current = []
                current_len = len(header)
            current.append(rendered)
            current_len += rendered_len
        if current:
            chunks.append(header + "\n" + "\n".join(current))
        return chunks

    async def deliver(
        self,
        signals: list[Signal],
        *,
        dry_run: bool,
        btc_bias: str | None = None,
        tier_by_tracking_id: dict[str, str] | None = None,
    ) -> list[DeliveredSignal]:
        delivered: list[DeliveredSignal] = []
        for signal in signals:
            contract_issues = validate_signal_contract(signal)
            if contract_issues:
                issue_codes = ",".join(f"{issue.field}.{issue.reason}" for issue in contract_issues)
                LOG.error(
                    (
                        "blocked direct signal delivery with invalid contract | "
                        "symbol=%s setup=%s issues=%s"
                    ),
                    signal.symbol,
                    signal.setup_id,
                    issue_codes,
                )
                delivered.append(
                    DeliveredSignal(
                        signal=signal,
                        status="rejected_contract",
                        message_id=None,
                        reason=issue_codes,
                    )
                )
                continue
            delivery_tier = str((tier_by_tracking_id or {}).get(signal.tracking_id) or "action")
            try:
                text = format_signal_text(
                    signal,
                    pending_expiry_minutes=self.pending_expiry_minutes,
                    btc_bias=btc_bias,
                    tier=delivery_tier,
                )
            except DEFENSIVE_EXC as exc:
                LOG.exception(
                    "failed to format signal text | symbol=%s setup=%s",
                    signal.symbol,
                    signal.setup_id,
                )
                delivered.append(
                    DeliveredSignal(
                        signal=signal,
                        status="format_error",
                        message_id=None,
                        reason=str(exc),
                    )
                )
                continue

            validation = validate_telegram_html(text)
            if not validation.ok:
                LOG.warning(
                    (
                        "telegram html validation failed, using safe fallback | "
                        "symbol=%s setup=%s issues=%s"
                    ),
                    signal.symbol,
                    signal.setup_id,
                    [issue.code for issue in validation.issues if issue.severity == "error"],
                )
                text = format_safe_signal_fallback(
                    signal,
                    pending_expiry_minutes=self.pending_expiry_minutes,
                    tier=delivery_tier,
                )

            if dry_run:
                LOG.info("dry-run signal\n%s", text)
                delivered.append(
                    DeliveredSignal(signal=signal, status="sent", message_id=None, reason="dry_run")
                )
                continue
            try:
                result = await self.broadcaster.send_html(text)
            except DEFENSIVE_EXC as exc:
                LOG.exception(
                    "signal delivery send failed | symbol=%s setup=%s",
                    signal.symbol,
                    signal.setup_id,
                )
                delivered.append(
                    DeliveredSignal(
                        signal=signal,
                        status="send_error",
                        message_id=None,
                        reason=str(exc),
                    )
                )
                continue
            audit = self._public_audit
            if audit is not None and result.status == "sent":
                audit.append_delivered(
                    signal,
                    tier=delivery_tier,
                    message_id=result.message_id,
                )
            if result.status == "sent":
                LOG.info("telegram signal sent\n%s", text)
            elif result.status == "logged":
                LOG.warning(
                    "signal not sent to Telegram (local/log only) | reason=%s symbol=%s setup=%s",
                    result.reason,
                    signal.symbol,
                    signal.setup_id,
                )
                LOG.debug("local signal logged\n%s", text)
            else:
                LOG.error(
                    "signal delivery status is not sent | status=%s reason=%s symbol=%s setup=%s",
                    result.status,
                    result.reason,
                    signal.symbol,
                    signal.setup_id,
                )
            delivered.append(
                DeliveredSignal(
                    signal=signal,
                    status=result.status,
                    message_id=result.message_id,
                    reason=result.reason,
                )
            )
        return delivered

    async def send_analytics_companion(
        self,
        signal: Signal,
        *,
        btc_bias: str | None = None,
        eth_bias: str | None = None,
    ) -> None:
        """Send a short analytics narrative as a follow-up message after a signal."""
        try:
            text = format_analytics_companion(signal, btc_bias=btc_bias, eth_bias=eth_bias)
            await self.broadcaster.send_html(text)
        except DEFENSIVE_EXC as exc:
            LOG.debug("analytics companion send failed: %s", exc)

    async def deliver_tracking_updates(
        self,
        events: list[SignalTrackingEvent],
        *,
        dry_run: bool,
    ) -> None:
        event_batches = self._coalesce_tracking_events(events)
        for batch in event_batches:
            final_event = batch[-1]
            tracked_card = format_tracked_signal_text(final_event.tracked)
            if len(batch) > 1:
                LOG.info(
                    "coalesced tracking batch | tracking_ref=%s events=%s final=%s",
                    final_event.tracked.tracking_ref,
                    [item.event_type for item in batch],
                    final_event.event_type,
                )
            if not dry_run and final_event.tracked.signal_message_id:
                try:
                    await self.broadcaster.edit_html(
                        final_event.tracked.signal_message_id, tracked_card
                    )
                    LOG.info("telegram signal card edited\n%s", tracked_card)
                except DEFENSIVE_EXC:
                    LOG.exception(
                        "telegram signal card edit failed for %s",
                        final_event.tracked.tracking_ref,
                    )
            elif dry_run:
                LOG.info("dry-run signal card edit\n%s", tracked_card)
            if not self._should_send_tracking_follow_up(final_event):
                continue
            text = format_tracking_event_text(final_event)
            if dry_run:
                LOG.info("dry-run tracking update\n%s", text)
                continue
            reply_to_message_id = final_event.tracked.signal_message_id or None
            result = await self.broadcaster.send_html(text, reply_to_message_id=reply_to_message_id)
            if result.status == "sent":
                LOG.info("telegram tracking update sent\n%s", text)
            else:
                LOG.debug(
                    (
                        "telegram tracking update not delivered | status=%s reason=%s "
                        "tracking_ref=%s event=%s"
                    ),
                    result.status,
                    result.reason,
                    final_event.tracked.tracking_ref,
                    final_event.event_type,
                )

    @staticmethod
    def _coalesce_tracking_events(
        events: list[SignalTrackingEvent],
    ) -> list[list[SignalTrackingEvent]]:
        grouped: OrderedDict[str, list[SignalTrackingEvent]] = OrderedDict()
        for event in events:
            grouped.setdefault(event.tracked.tracking_id, []).append(event)
        return list(grouped.values())

    _CHANNEL_LIFECYCLE_EVENTS = frozenset(
        {
            "activated",
            "tp1_hit",
            "tp2_hit",
            "stop_loss",
            "expired",
        }
    )
    # Channel policy: always edit the original card first; lifecycle replies are supplemental.
    _EDIT_CARD_BEFORE_REPLY_EVENTS = _CHANNEL_LIFECYCLE_EVENTS

    def _should_send_tracking_follow_up(self, event: SignalTrackingEvent) -> bool:
        if event.event_type == "superseded":
            return False
        if event.event_type not in self._CHANNEL_LIFECYCLE_EVENTS:
            return False
        occurred_at = event.occurred_at.astimezone(UTC)
        max_age = timedelta(minutes=self.tracking_reply_freshness_minutes)
        if datetime.now(UTC) - occurred_at > max_age:
            LOG.info(
                "suppressing stale tracking follow-up | tracking_ref=%s event=%s age_minutes=%.1f",
                event.tracked.tracking_ref,
                event.event_type,
                (datetime.now(UTC) - occurred_at).total_seconds() / 60.0,
            )
            return False
        return True

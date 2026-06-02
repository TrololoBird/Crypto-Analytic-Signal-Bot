from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from bot.core.runtime_errors import DEFENSIVE_EXC

from ..persistence.tracking import SignalTrackingEvent
from .contract import validate_signal_contract
from .formatting import (
    format_analytics_companion_message,
    format_signal_message,
    format_tracked_signal_message,
    format_tracking_event_message,
)
from .telegram import DeliveryResult

if TYPE_CHECKING:
    from ..domain.schemas import Signal

LOG = logging.getLogger("bot.delivery.deliver")
LOCAL_TZ = datetime.now().astimezone().tzinfo or UTC
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


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "data_missing"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "data_invalid"
    if not math.isfinite(numeric):
        return "data_invalid"
    if numeric >= 1_000.0:
        return f"{numeric:,.2f}"
    if numeric >= 1.0:
        return f"{numeric:,.4f}"
    return f"{numeric:.6f}"


def _tz_label(value: datetime) -> str:
    offset = value.utcoffset() or timedelta(0)
    sign = "+" if offset >= timedelta(0) else "-"
    total_minutes = abs(int(offset.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _fmt_dt(raw: str | datetime | None) -> str:
    if raw is None:
        return "time_missing"
    try:
        value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return "time_invalid"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(LOCAL_TZ)
    return f"{value.strftime('%Y-%m-%d %H:%M')} {_tz_label(value)}"


def _direction_label(direction: str) -> str:
    return "LONG" if direction == "long" else "SHORT"


def _confluence_summary(reasons: tuple[str, ...] | list[str]) -> str | None:
    setup_count = 0
    setups: list[str] = []
    for reason in reasons:
        raw = str(reason or "").strip()
        if raw.startswith("confluence_") and raw.endswith("_setups"):
            count = raw.removeprefix("confluence_").removesuffix("_setups")
            if count.isdigit():
                setup_count = max(setup_count, int(count))
        elif raw.startswith("confluence_setups="):
            payload = raw.split("=", 1)[1]
            setups = [item.strip() for item in payload.split(",") if item.strip()]
    if not setup_count and len(setups) > 1:
        setup_count = len(setups)
    if setup_count <= 1:
        return None
    summary = f"{setup_count} setups"
    if setups:
        summary = f"{summary}: {', '.join(setups)}"
    return html.escape(summary)


def _fmt_audit_metric(name: str, value: float | None, suffix: str = "") -> str | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
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


def _status_line_for_tracked(tracked: SignalTrackingEvent | object) -> str:
    state = tracked.tracked if isinstance(tracked, SignalTrackingEvent) else tracked
    pending_expires_at = getattr(state, "pending_expires_at", None)
    activated_at = getattr(state, "activated_at", None)
    activation_price = getattr(state, "activation_price", None)
    tp1_hit_at = getattr(state, "tp1_hit_at", None)
    moved_to_break_even_at = getattr(state, "moved_to_break_even_at", None)
    close_reason = getattr(state, "close_reason", None)
    closed_at = getattr(state, "closed_at", None)
    close_price = getattr(state, "close_price", None)
    single_target_mode = bool(getattr(state, "single_target_mode", False))
    entry_mid = getattr(state, "entry_mid", None)
    activation_price = getattr(state, "activation_price", None)
    if close_reason == "tp1_hit" and single_target_mode:
        tp_px = _fmt_price(close_price or state.take_profit_1)
        return f"closed at TP on <code>{_fmt_dt(closed_at)}</code> at <code>{tp_px}</code>"
    if close_reason == "tp2_hit":
        tp_px = _fmt_price(close_price or state.take_profit_2)
        return f"closed at TP2 on <code>{_fmt_dt(closed_at)}</code> at <code>{tp_px}</code>"
    if close_reason == "stop_loss":
        # If stop was moved to break-even (TP1) and then hit, show it explicitly.
        break_even = activation_price or entry_mid
        stop_px = close_price or state.stop
        is_break_even = False
        if moved_to_break_even_at and break_even and stop_px:
            try:
                is_break_even = abs(float(stop_px) - float(break_even)) <= (
                    float(break_even) * 1e-6
                )
            except (TypeError, ValueError):
                is_break_even = False
        label = "stopped (break-even)" if is_break_even else "stopped"
        return f"{label} on <code>{_fmt_dt(closed_at)}</code> at <code>{_fmt_price(stop_px)}</code>"
    if close_reason == "smart_exit":
        exit_px = close_price or state.last_price or entry_mid
        return (
            "analytical smart-exit on "
            f"<code>{_fmt_dt(closed_at)}</code> at <code>{_fmt_price(exit_px)}</code>"
        )
    if close_reason == "emergency_exit":
        exit_px = close_price or state.last_price or entry_mid
        return (
            "analytical hard-barrier exit on "
            f"<code>{_fmt_dt(closed_at)}</code> at <code>{_fmt_price(exit_px)}</code>"
        )
    if close_reason == "expired":
        return f"expired on <code>{_fmt_dt(closed_at)}</code>"
    if close_reason == "ambiguous_exit":
        return f"ambiguous exit on <code>{_fmt_dt(closed_at)}</code>"
    if tp1_hit_at:
        if single_target_mode:
            return f"TP hit on <code>{_fmt_dt(tp1_hit_at)}</code>"
        if moved_to_break_even_at:
            return f"TP1 hit on <code>{_fmt_dt(tp1_hit_at)}</code>; stop moved to break-even"
        return f"TP1 hit on <code>{_fmt_dt(tp1_hit_at)}</code>; TP2 still open"
    if activated_at:
        entry_px = _fmt_price(activation_price or state.entry_mid)
        return f"active since <code>{_fmt_dt(activated_at)}</code> at <code>{entry_px}</code>"
    return f"waiting entry until <code>{_fmt_dt(pending_expires_at)}</code>"


def _confidence_label(score: float) -> str:
    if score > 0.75:
        return "strong"
    if score >= 0.68:
        return "medium"
    return "moderate"


def _render_signal_card(
    *,
    symbol: str,
    direction: str,
    tracking_ref: str,
    timeframe: str,
    setup_id: str,
    entry_low: float,
    entry_high: float,
    stop: float,
    take_profit_1: float,
    take_profit_2: float,
    take_profit_3: float | None = None,
    risk_reward: float,
    stop_distance_pct: float,
    reasons: tuple[str, ...] | list[str],
    status_line: str,
    score: float = 0.0,
    scale_weights: tuple[float, float, float] | list[float] | None = None,
    oi_change_pct: float | None = None,
    funding_rate: float | None = None,
    btc_bias: str | None = None,
    expiry_dt: datetime | None = None,
) -> str:
    entry_mid = (entry_low + entry_high) / 2.0
    tp3 = float(take_profit_3 or take_profit_2)
    scale = max(abs(entry_mid), abs(take_profit_1), abs(take_profit_2), abs(tp3), 1.0)
    single_target_mode = abs(take_profit_2 - take_profit_1) <= (scale * 1e-6)
    weights = tuple(scale_weights or (0.5, 0.3, 0.2))
    if len(weights) != 3:
        weights = (0.5, 0.3, 0.2)
    weight_labels = tuple(round(float(weight) * 100.0) for weight in weights)

    lines: list[str] = []

    if btc_bias == "downtrend" and direction == "long":
        lines.append("<b>BTC risk</b> <code>downtrend vs LONG</code>")
    elif btc_bias == "uptrend" and direction == "short":
        lines.append("<b>BTC risk</b> <code>uptrend vs SHORT</code>")

    direction_label = _direction_label(direction)
    symbol_html = html.escape(symbol)
    lines += [
        (f"<b>LIMIT {direction_label} {symbol_html}</b> <code>#{tracking_ref}</code>"),
        (
            f"<b>Setup</b> <code>{html.escape(setup_id)} {html.escape(timeframe)}</code> | "
            f"<b>Score</b> <code>{score * 100:.0f}% {_confidence_label(score)}</code>"
        ),
        f"<b>Entry</b> <code>{_fmt_price(entry_low)} - {_fmt_price(entry_high)}</code>",
        f"<b>SL</b> <code>{_fmt_price(stop)}</code>",
        (
            f"<b>TP</b> <code>{_fmt_price(take_profit_1)}</code>"
            if single_target_mode
            else (
                f"<b>TP</b> TP1 <code>{_fmt_price(take_profit_1)}</code> | "
                f"TP2 <code>{_fmt_price(take_profit_2)}</code> | "
                f"TP3 <code>{_fmt_price(tp3)}</code>"
            )
        ),
        (
            f"<b>Scale</b> <code>{weight_labels[0]}% / {weight_labels[1]}% / "
            f"{weight_labels[2]}%</code>"
        ),
        (
            f"<b>RR</b> <code>{risk_reward:.2f}</code> | "
            f"<b>Risk</b> <code>{stop_distance_pct:.2f}%</code>"
        ),
    ]
    ctx = _market_context_line(oi_change_pct, funding_rate)
    if ctx:
        lines.append(f"<b>Market</b> <code>{html.escape(ctx)}</code>")
    confluence = _confluence_summary(reasons)
    if confluence:
        lines.append(f"<b>Confluence</b> <code>{confluence}</code>")
    chart_url = html.escape(tradingview_chart_url(symbol, timeframe), quote=True)
    lines.append(f'<b>Chart</b> <a href="{chart_url}">TradingView</a>')
    if expiry_dt:
        lines.append(f"<b>Wait entry until</b> <code>{_fmt_dt(expiry_dt)}</code>")
    else:
        lines.append(f"<b>Status</b> {status_line}")
    return "\n".join(lines)


def _market_context_line(oi_change_pct: float | None, funding_rate: float | None) -> str | None:
    parts = []
    if oi_change_pct is not None:
        sign = "+" if oi_change_pct >= 0 else ""
        parts.append(f"OI {sign}{oi_change_pct * 100:.1f}%")
    if funding_rate is not None:
        sign = "+" if funding_rate >= 0 else ""
        parts.append(f"FR {sign}{funding_rate * 100:.4f}%")
    return " | ".join(parts) if parts else None


def format_signal_text(
    signal: Signal, *, pending_expiry_minutes: int, btc_bias: str | None = None
) -> str:
    try:
        return format_signal_message(
            signal,
            pending_expiry_minutes=pending_expiry_minutes,
            btc_bias=btc_bias,
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
            try:
                text = format_signal_text(
                    signal,
                    pending_expiry_minutes=self.pending_expiry_minutes,
                    btc_bias=btc_bias,
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

            if dry_run:
                LOG.info("dry-run signal\n%s", text)
                delivered.append(
                    DeliveredSignal(signal=signal, status="sent", message_id=None, reason="dry_run")
                )
                continue
            delivery_tier = str((tier_by_tracking_id or {}).get(signal.tracking_id) or "action")
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
            edited = False
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
                    edited = True
                except DEFENSIVE_EXC:
                    LOG.exception(
                        "telegram signal card edit failed for %s",
                        final_event.tracked.tracking_ref,
                    )
            elif dry_run:
                LOG.info("dry-run signal card edit\n%s", tracked_card)
                edited = True
            if not self._should_send_tracking_follow_up(final_event):
                continue
            if final_event.event_type == "activated" and edited:
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

    def _should_send_tracking_follow_up(self, event: SignalTrackingEvent) -> bool:
        if event.event_type == "activated":
            return False
        if event.event_type == "superseded":
            return False
        if event.event_type == "expired" and not getattr(event.tracked, "activated_at", None):
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

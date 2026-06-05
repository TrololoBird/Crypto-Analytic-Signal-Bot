"""Telegram operator console — remote monitoring from any network (no LAN required).

Authorized users DM the bot with /status, /sl, /tracking, etc. Works behind NAT on
MacBook via long-polling getUpdates. Configure TELEGRAM_OPERATOR_USER_IDS in .env.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from bot.domain.labels import tracking_event_ru
from bot.domain.limit_entry import resolve_late_entry_chase_pct
from bot.runtime.delivery_session import delivery_session_snapshot
from bot.runtime.errors import DEFENSIVE_EXC, defensive_exc_types

from ..dashboard.mobile_summary import (
    build_mobile_summary,
    format_mobile_digest_text,
    format_operator_help_text,
    format_operator_sl_text,
    format_operator_status_text,
)
from ..dashboard.operator_actions import (
    action_analyze_symbol,
    action_refresh_market_context,
    action_refresh_shortlist,
    action_review_tracking,
    action_scan_market,
    format_action_result_html,
    format_signal_detail_html,
    format_symbol_lookup_html,
    lookup_signal_by_ref,
    lookup_symbol_signals,
    normalize_operator_symbol,
)
from ..dashboard.operator_context import (
    format_operator_audit_text,
    format_operator_cycles_text,
    format_operator_health_text,
    format_operator_policy_text,
    resolve_operator_market_html,
)
from ..dashboard.user_summary import build_user_summary, reject_reason_ru
from ..persistence.db_status import (
    collect_db_status,
    collect_db_status_from_conn,
    format_db_status_html,
)
from .startup_digest import format_startup_tracking_digest

if TYPE_CHECKING:
    from ..runtime.bot import SignalBot

LOG = logging.getLogger("bot.telegram_operator")
_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def operator_console_enabled(bot: SignalBot) -> bool:
    settings = bot.settings
    op_cfg = getattr(settings.notifiers, "telegram_operator", None)
    if op_cfg is not None and not bool(getattr(op_cfg, "enabled", True)):
        return False
    token = str(getattr(settings, "tg_token", "") or "").strip()
    ids = tuple(getattr(settings, "operator_user_ids", ()) or ())
    return bool(token and ids)


class TelegramOperatorConsole:
    """Long-poll Telegram updates and answer operator commands in private chat."""

    def __init__(self, bot: SignalBot) -> None:
        self._bot = bot
        self._offset: int | None = None
        self._session: aiohttp.ClientSession | None = None
        self._denied_logged: set[int] = set()
        self._action_lock = asyncio.Lock()

    @property
    def _token(self) -> str:
        return str(getattr(self._bot.settings, "tg_token", "") or "").strip()

    @property
    def _operator_ids(self) -> frozenset[int]:
        return frozenset(
            int(x) for x in (getattr(self._bot.settings, "operator_user_ids", ()) or ())
        )

    @property
    def _poll_timeout(self) -> int:
        op_cfg = getattr(self._bot.settings.notifiers, "telegram_operator", None)
        return int(getattr(op_cfg, "poll_timeout_seconds", 25) or 25)

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(force_close=True, enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=max(35, self._poll_timeout + 10)),
            )
        return self._session

    async def _reset_session(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _api_call(self, method: str, **payload: Any) -> dict[str, Any]:
        url = _API_BASE.format(token=self._token, method=method)
        session = await self._session_get()
        async with session.post(url, json=payload) as resp:
            data = await resp.json(content_type=None)
        if not isinstance(data, dict):
            return {"ok": False, "description": "invalid_response"}
        return data

    async def send_html(self, chat_id: int, text: str) -> bool:
        if len(text) > 3900:
            text = text[:3890] + "…"
        try:
            result = await self._api_call(
                "sendMessage",
                chat_id=int(chat_id),
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return bool(result.get("ok"))
        except DEFENSIVE_EXC:
            LOG.debug("operator send failed chat_id=%s", chat_id, exc_info=True)
            return False

    async def send_html_to_operators(self, text: str) -> int:
        sent = 0
        for user_id in sorted(self._operator_ids):
            if await self.send_html(user_id, text):
                sent += 1
        return sent

    def _authorized(self, user_id: int | None) -> bool:
        return user_id is not None and int(user_id) in self._operator_ids

    async def _reply_unauthorized(self, chat_id: int, user_id: int | None) -> None:
        if user_id is not None and user_id not in self._denied_logged:
            self._denied_logged.add(user_id)
            LOG.warning("telegram operator access denied | user_id=%s", user_id)
        await self.send_html(
            chat_id,
            "<b>Доступ запрещён</b>\n"
            "Ваш Telegram user id не в <code>TELEGRAM_OPERATOR_USER_IDS</code>.",
        )

    async def _build_payload(self) -> dict[str, Any]:
        dashboard = getattr(self._bot, "dashboard", None)
        live_data = getattr(dashboard, "_live_data", None) if dashboard is not None else None
        if live_data is None:
            return {"error": "live_data_unavailable"}
        return await build_mobile_summary(self._bot, live_data)

    async def _handle_command(self, chat_id: int, text: str) -> None:
        parts = text.strip().split()
        cmd = (parts[0] if parts else "").lower()
        if "@" in cmd:
            cmd = cmd.split("@", 1)[0]
        args = parts[1:]
        arg0 = args[0].lower() if args else ""

        if cmd in {"/start", "/help"}:
            await self.send_html(chat_id, format_operator_help_text())
            return

        if cmd in {"/market", "/context", "/ctx"}:
            if arg0 in {"refresh", "update", "reload"}:
                await self._run_action(
                    chat_id,
                    "Обновление market context",
                    lambda: action_refresh_market_context(self._bot),
                    timeout_s=120.0,
                )
                return
            text = await resolve_operator_market_html(self._bot, tag="operator")
            await self.send_html(chat_id, text)
            return

        if cmd in {"/health", "/ping"}:
            await self.send_html(chat_id, await format_operator_health_text(self._bot))
            return

        if cmd == "/audit":
            live_data = getattr(getattr(self._bot, "dashboard", None), "_live_data", None)
            await self.send_html(chat_id, await format_operator_audit_text(self._bot, live_data))
            return

        if cmd in {"/policy", "/config"}:
            await self.send_html(chat_id, format_operator_policy_text(self._bot))
            return

        if cmd in {"/symbol", "/coin", "/s"}:
            sym = normalize_operator_symbol(
                " ".join(args),
                quote_asset=self._bot.settings.universe.quote_asset,
            )
            if not sym:
                await self.send_html(chat_id, "<b>Usage:</b> <code>/symbol BTCUSDT</code>")
                return
            rows = await lookup_symbol_signals(self._bot, sym)
            await self.send_html(chat_id, format_symbol_lookup_html(self._bot, sym, rows))
            return

        if cmd == "/signal":
            if not args:
                await self.send_html(
                    chat_id,
                    "<b>Usage:</b> <code>/signal REF</code> или <code>/signal BTCUSDT</code>",
                )
                return
            row = await lookup_signal_by_ref(self._bot, args[0])
            if row is None:
                await self.send_html(
                    chat_id, f"Сигнал не найден: <code>{html.escape(args[0])}</code>"
                )
                return
            await self.send_html(chat_id, format_signal_detail_html(self._bot, row))
            return

        if cmd == "/review":
            sym = normalize_operator_symbol(
                " ".join(args),
                quote_asset=self._bot.settings.universe.quote_asset,
            )
            if not sym:
                await self.send_html(chat_id, "<b>Usage:</b> <code>/review BTCUSDT</code>")
                return
            await self._run_action(
                chat_id,
                f"Tracking review {sym}",
                lambda: action_review_tracking(self._bot, sym),
                timeout_s=90.0,
            )
            return

        if cmd == "/analyze":
            sym = normalize_operator_symbol(
                " ".join(args),
                quote_asset=self._bot.settings.universe.quote_asset,
            )
            if not sym:
                await self.send_html(chat_id, "<b>Usage:</b> <code>/analyze BTCUSDT</code>")
                return
            await self._run_action(
                chat_id,
                f"Analyze {sym}",
                lambda: action_analyze_symbol(self._bot, sym),
                timeout_s=120.0,
            )
            return

        if cmd == "/scan":
            if args:
                sym = normalize_operator_symbol(
                    " ".join(args),
                    quote_asset=self._bot.settings.universe.quote_asset,
                )
                if not sym:
                    await self.send_html(
                        chat_id, "<b>Usage:</b> <code>/scan</code> или <code>/scan BTC</code>"
                    )
                    return
                await self._run_action(
                    chat_id,
                    f"Analyze {sym}",
                    lambda: action_analyze_symbol(self._bot, sym),
                    timeout_s=120.0,
                )
                return
            await self._run_action(
                chat_id,
                "Полный скан shortlist",
                lambda: action_scan_market(self._bot),
                timeout_s=600.0,
            )
            return

        if cmd == "/refresh":
            if arg0 in {"", "shortlist", "sl"}:
                await self._run_action(
                    chat_id,
                    "Refresh shortlist",
                    lambda: action_refresh_shortlist(self._bot),
                    timeout_s=180.0,
                )
                return
            if arg0 in {"market", "context", "ctx"}:
                await self._run_action(
                    chat_id,
                    "Refresh market context",
                    lambda: action_refresh_market_context(self._bot),
                    timeout_s=120.0,
                )
                return
            await self.send_html(
                chat_id,
                "<b>Usage:</b> <code>/refresh shortlist</code> · <code>/refresh market</code>",
            )
            return

        payload = await self._build_payload()
        if payload.get("error"):
            await self.send_html(chat_id, "<b>Бот ещё прогревается</b> — повторите через минуту.")
            return

        if cmd == "/status":
            status_text = format_operator_status_text(payload)
            db_text = await self._format_db_status_text()
            await self.send_html(chat_id, f"{status_text}\n\n{db_text}")
            return
        if cmd == "/digest":
            await self.send_html(chat_id, format_mobile_digest_text(payload))
            return
        if cmd in {"/sl", "/stoploss", "/stops"}:
            await self.send_html(chat_id, format_operator_sl_text(payload))
            return
        if cmd in {"/outcomes", "/stats"}:
            await self.send_html(
                chat_id, format_operator_status_text(payload, detail_outcomes=True)
            )
            return
        if cmd == "/shortlist":
            if arg0 in {"refresh", "update", "reload"}:
                await self._run_action(
                    chat_id,
                    "Refresh shortlist",
                    lambda: action_refresh_shortlist(self._bot),
                    timeout_s=180.0,
                )
                return
            await self._reply_shortlist(chat_id, payload)
            return
        if cmd == "/tracking":
            await self._reply_tracking(chat_id, detail=True)
            return
        if cmd in {"/cycles", "/pipeline"}:
            overview: dict[str, Any] = {}
            live_data = getattr(getattr(self._bot, "dashboard", None), "_live_data", None)
            if live_data is not None:
                try:
                    overview = live_data.overview() or {}
                except DEFENSIVE_EXC:
                    overview = {}
            await self.send_html(chat_id, format_operator_cycles_text(payload, overview))
            return
        if cmd in {"/signals", "/today"}:
            await self._reply_signals(chat_id, payload)
            return
        if cmd in {"/open", "/positions"}:
            await self._reply_tracking(chat_id, detail=True)
            return
        if cmd == "/pending":
            await self._reply_tracking(chat_id, status_filter="pending", detail=True)
            return
        if cmd == "/active":
            await self._reply_tracking(chat_id, status_filter="active", detail=True)
            return
        if cmd == "/rejections":
            await self._reply_rejections(chat_id)
            return
        if cmd == "/funnel":
            await self._reply_funnel(chat_id)
            return
        if cmd == "/gates":
            await self._reply_gates(chat_id)
            return
        if cmd in {"/strategies", "/setups"}:
            await self._reply_strategies(chat_id)
            return
        if cmd in {"/delivery", "/telegram"}:
            await self._reply_delivery(chat_id, payload)
            return
        if cmd in {"/notify", "/alerts"}:
            await self._reply_notify_flags(chat_id)
            return

        await self.send_html(
            chat_id,
            "Неизвестная команда. Используйте /help",
        )

    async def _format_db_status_text(self) -> str:
        repo = getattr(self._bot, "_modern_repo", None)
        if repo is not None:
            try:
                summary = await collect_db_status_from_conn(repo._require_conn())
                return format_db_status_html(summary)
            except DEFENSIVE_EXC:
                LOG.debug("operator db status via live repo failed", exc_info=True)
        try:
            summary = await collect_db_status(self._bot.settings)
            return format_db_status_html(summary)
        except DEFENSIVE_EXC:
            LOG.debug("operator db status failed", exc_info=True)
            return "<b>DB</b>\nUnavailable"

    async def _run_action(
        self,
        chat_id: int,
        label: str,
        factory: Any,
        *,
        timeout_s: float,
    ) -> None:
        if self._action_lock.locked():
            await self.send_html(
                chat_id,
                "<b>Занято</b> — другая operator-команда уже выполняется. Подождите.",
            )
            return
        await self.send_html(chat_id, f"⏳ {html.escape(label)}…")
        async with self._action_lock:
            try:
                result = await asyncio.wait_for(factory(), timeout=timeout_s)
            except TimeoutError:
                await self.send_html(
                    chat_id,
                    f"<b>{html.escape(label)}</b>\n❌ timeout {int(timeout_s)}s",
                )
                return
            except DEFENSIVE_EXC:
                LOG.exception("operator action failed | label=%s", label)
                await self.send_html(chat_id, f"<b>{html.escape(label)}</b>\n❌ internal error")
                return
        await self.send_html(chat_id, format_action_result_html(label, result))

    async def _reply_shortlist(self, chat_id: int, payload: dict[str, Any]) -> None:
        runtime = payload.get("runtime") or {}
        symbols = list(getattr(self._bot, "_last_live_shortlist", []) or [])[:12]
        lines = [
            "<b>Shortlist</b>",
            f"Size: <code>{runtime.get('shortlist_size') or len(symbols)}</code>",
            f"Regime: <code>{runtime.get('regime') or '—'}</code> · "
            f"BTC: <code>{runtime.get('btc_bias') or '—'}</code>",
        ]
        if symbols:
            preview = ", ".join(
                str(getattr(s, "symbol", s) if not isinstance(s, str) else s) for s in symbols[:10]
            )
            lines.append(f"Top: <code>{html.escape(preview)}</code>")
        await self.send_html(chat_id, "\n".join(lines))

    async def _reply_tracking(
        self,
        chat_id: int,
        *,
        detail: bool = False,
        status_filter: str | None = None,
    ) -> None:
        repo = getattr(self._bot, "_modern_repo", None)
        if repo is None:
            await self.send_html(chat_id, "<b>Tracking</b>\nНет данных.")
            return
        try:
            rows = await repo.get_active_signals()
        except DEFENSIVE_EXC:
            await self.send_html(chat_id, "<b>Tracking</b>\nОшибка чтения БД.")
            return
        pending = [r for r in rows if str(r.get("status") or "") == "pending"]
        active = [r for r in rows if str(r.get("status") or "") == "active"]
        if status_filter == "pending":
            show = pending
        elif status_filter == "active":
            show = active
        else:
            show = pending + active
        lines = [
            "<b>Tracking (signal-only)</b>",
            ("<i>pending</i> = ждём касание зоны входа · <i>active</i> = в сделке, SL/TP в канале"),
            f"Pending: <code>{len(pending)}</code> · Active: <code>{len(active)}</code>",
        ]
        limit = 10 if detail else 6
        for row in show[:limit]:
            sym = html.escape(str(row.get("symbol") or "?"))
            direction = html.escape(str(row.get("direction") or "?"))
            setup = html.escape(str(row.get("setup_type") or row.get("setup_id") or "?"))
            status = html.escape(str(row.get("status") or "?"))
            el = row.get("entry_low")
            eh = row.get("entry_high")
            sl = row.get("stop") or row.get("stop_price")
            lp = row.get("last_price")
            zone_at = row.get("entry_zone_touched_at")
            act_at = row.get("activated_at")
            extra = ""
            if detail and el is not None and eh is not None:
                extra = f" · entry <code>{el}</code>-<code>{eh}</code>"
            if detail and sl is not None:
                extra += f" · SL <code>{sl}</code>"
            if detail and lp is not None:
                extra += f" · last <code>{lp}</code>"
            if detail and zone_at and not act_at:
                extra += f" · <i>{html.escape(tracking_event_ru('entry_zone_touched'))}</i>"
            elif detail and act_at:
                extra += f" · <i>{html.escape(tracking_event_ru('activated'))}</i>"
            lines.append(f"• {sym} {direction} · {setup} · <code>{status}</code>{extra}")
        if len(show) > limit:
            lines.append(f"<i>…ещё {len(show) - limit}</i>")
        await self.send_html(chat_id, "\n".join(lines))

    async def _reply_gates(self, chat_id: int) -> None:
        await self._reply_funnel(chat_id)
        await self._reply_rejections(chat_id)

    async def _reply_funnel(self, chat_id: int) -> None:
        dashboard = getattr(self._bot, "dashboard", None)
        live_data = getattr(dashboard, "_live_data", None) if dashboard is not None else None
        if live_data is None:
            await self.send_html(chat_id, "<b>Funnel</b>\nНет live данных.")
            return
        try:
            funnel = live_data.funnel(max_rows=50_000)
        except DEFENSIVE_EXC:
            await self.send_html(chat_id, "<b>Funnel</b>\nОшибка чтения telemetry.")
            return
        totals = funnel.get("cycle_totals") if isinstance(funnel.get("cycle_totals"), dict) else {}
        stages = funnel.get("stages") if isinstance(funnel.get("stages"), list) else []
        lines = [
            "<b>Funnel (сегодня)</b>",
            (
                f"Candidates: <code>{int(totals.get('candidate_count') or 0)}</code> · "
                f"Delivered: <code>{int(totals.get('delivered_count') or 0)}</code>"
            ),
        ]
        for stage in stages[:8]:
            if not isinstance(stage, dict):
                continue
            label = html.escape(str(stage.get("label") or stage.get("stage") or "?"))
            count = int(stage.get("count") or 0)
            lines.append(f"• {label}: <code>{count}</code>")
        await self.send_html(chat_id, "\n".join(lines))

    async def _reply_strategies(self, chat_id: int) -> None:
        registry = getattr(self._bot, "_modern_registry", None)
        enabled = list(self._bot.settings.setups.enabled_setup_ids())
        lines = [
            "<b>Strategies</b>",
            f"Enabled: <code>{len(enabled)}</code> / registry "
            f"<code>{len(registry) if registry else '—'}</code>",
            "<i>MTF: детектор на 15m, фильтры 1h/4h в pipeline + confluence</i>",
        ]
        preview = ", ".join(html.escape(s) for s in enabled[:12])
        if preview:
            lines.append(f"<code>{preview}</code>")
        await self.send_html(chat_id, "\n".join(lines))

    async def _reply_delivery(self, chat_id: int, payload: dict[str, Any]) -> None:
        today = payload.get("today") or {}
        chase = resolve_late_entry_chase_pct(self._bot.settings)
        snap = delivery_session_snapshot(self._bot)
        lines = [
            "<b>Delivery</b>",
            "Channel: ACTION/WATCH + lifecycle (zone → active → TP/SL)",
            f"Session delivered: <code>{today.get('session_delivered') or 0}</code>",
        ]
        cap = int(snap.get("session_action_cap") or 0)
        if cap > 0:
            lines.append(
                f"Session ACTION cap: <code>{snap.get('session_action_delivered') or 0}</code>"
                f"/<code>{cap}</code>"
            )
        lines += [
            f"Late-entry chase cap: <code>{chase * 100:.2f}%</code> past zone",
            f"Pending TTL: <code>{self._bot.settings.tracking.pending_expiry_minutes}m</code>",
        ]
        await self.send_html(chat_id, "\n".join(lines))

    async def _reply_notify_flags(self, chat_id: int) -> None:
        op = getattr(self._bot.settings.notifiers, "telegram_operator", None)
        flags = [
            "send_market_context",
            "send_startup_report",
            "send_digest",
            "send_sl_postmortem",
        ]
        lines = ["<b>Operator notify flags</b>"]
        for name in flags:
            val = bool(getattr(op, name, True)) if op is not None else True
            lines.append(f"{html.escape(name)}: <code>{'on' if val else 'off'}</code>")
        lines.append("<i>Канал: только сигналы и lifecycle — ops в личку.</i>")
        await self.send_html(chat_id, "\n".join(lines))

    async def _reply_signals(self, chat_id: int, payload: dict[str, Any]) -> None:
        today = payload.get("today") or {}
        outcomes = payload.get("outcomes_7d") or {}
        lines = [
            "<b>Signals today</b>",
            f"Sent: <code>{today.get('signals_sent') or 0}</code> · "
            f"delivered session <code>{today.get('session_delivered') or 0}</code>",
            f"Pending/active: <code>{today.get('pending') or 0}</code>/"
            f"<code>{today.get('active') or 0}</code>",
            f"Rejections logged: <code>{today.get('rejections') or 0}</code>",
            (
                f"7d WR: <code>{round(float(outcomes.get('win_rate') or 0) * 100, 1)}%</code> · "
                f"SL <code>{outcomes.get('stop_losses') or 0}</code>"
            ),
        ]
        await self.send_html(chat_id, "\n".join(lines))

    async def _reply_rejections(self, chat_id: int) -> None:
        dashboard = getattr(self._bot, "dashboard", None)
        live_data = getattr(dashboard, "_live_data", None) if dashboard is not None else None
        if live_data is None:
            await self.send_html(chat_id, "<b>Funnel</b>\nНет live данных.")
            return
        summary = await build_user_summary(self._bot, live_data)
        hint = summary.get("funnel_hint") or {}
        lines = ["<b>Rejections</b>", html.escape(str(hint.get("text") or "—"))]
        top_key = hint.get("top_filter")
        top_count = int(hint.get("top_filter_count") or 0)
        if top_key and top_count:
            lines.append(
                f"Top: {html.escape(reject_reason_ru(str(top_key)))}: <code>{top_count}</code>"
            )
        await self.send_html(chat_id, "\n".join(lines))

    async def run_forever(self, *, stop_event: asyncio.Event) -> None:
        if not operator_console_enabled(self._bot):
            LOG.info(
                "telegram operator console disabled | need tg_token + TELEGRAM_OPERATOR_USER_IDS"
            )
            await stop_event.wait()
            return

        LOG.info(
            "telegram operator console started | operators=%s commands=/help",
            sorted(self._operator_ids),
        )
        op_cfg = getattr(self._bot.settings.notifiers, "telegram_operator", None)
        if bool(getattr(op_cfg, "notify_on_console_start", True)):
            await self.send_html_to_operators(
                "<b>Operator console</b> активен.\n"
                "<b>Контроль:</b> /symbol · /signal · /analyze · /scan · /refresh\n"
                "<b>Мониторинг:</b> /market · /status · /health · /tracking\n"
                "/help — полный список"
            )
        summary = getattr(self._bot, "_startup_tracking_summary", None)
        if summary and bool(getattr(op_cfg, "send_startup_report", True)):
            await self.send_html_to_operators(format_startup_tracking_digest(summary))

        while not stop_event.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except defensive_exc_types(aiohttp.ClientError):
                LOG.debug("operator poll error", exc_info=True)
                await self._reset_session()
                await asyncio.sleep(3.0)

        await self.close()

    async def _poll_once(self) -> None:
        params: dict[str, Any] = {
            "timeout": self._poll_timeout,
            "allowed_updates": ["message"],
        }
        if self._offset is not None:
            params["offset"] = self._offset

        url = _API_BASE.format(token=self._token, method="getUpdates")
        try:
            session = await self._session_get()
            async with session.get(url, params=params) as resp:
                data = await resp.json(content_type=None)
        except defensive_exc_types(aiohttp.ClientError):
            await self._reset_session()
            raise

        if not isinstance(data, dict) or not data.get("ok"):
            await asyncio.sleep(2.0)
            return

        for update in data.get("result") or []:
            if not isinstance(update, dict):
                continue
            update_id = int(update.get("update_id", 0))
            self._offset = update_id + 1
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            text = str(message.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            chat_id = int(chat.get("id") or 0)
            from_user = message.get("from") if isinstance(message.get("from"), dict) else {}
            user_id = int(from_user.get("id") or 0) or None
            if not self._authorized(user_id):
                await self._reply_unauthorized(chat_id, user_id)
                continue
            await self._handle_command(chat_id, text)

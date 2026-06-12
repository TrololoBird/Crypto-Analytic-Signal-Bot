"""Hunt Telegram command loop — /signal <SYMBOL> on-demand probe."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp
import structlog

from hunt_core.domain.config import load_settings
from hunt_core.errors import DEFENSIVE_EXC, defensive_exc_types
from hunt_core.secrets import load_secrets
from hunt_core.telegram import TelegramBroadcaster

from hunt_watch.signals_report import deliver_signals_report
from hunt_watch.autotune_runner import format_autotune_html, run_autotune
from hunt_watch.stats_report import deliver_stats_report
from hunt_watch.prep_shadow_tracker import format_prep_shadow_html, summarize_prep_shadows
from hunt_watch.symbol_probe import deliver_signal_probe, normalize_symbol, parse_symbol_text

LOG = structlog.get_logger("hunt.telegram_commands")
_API = "https://api.telegram.org/bot{token}/{method}"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,16}(USDT|USDC)?$")


class HuntTelegramCommands:
    """Long-poll /signal without blocking the hunt watch loop."""

    def __init__(
        self,
        token: str,
        *,
        allowed_chat_ids: frozenset[int],
        allowed_user_ids: frozenset[int],
        poll_timeout: int = 25,
    ) -> None:
        self._token = token
        self._allowed_chat_ids = allowed_chat_ids
        self._allowed_user_ids = allowed_user_ids
        self._poll_timeout = poll_timeout
        self._offset: int | None = None
        self._session: aiohttp.ClientSession | None = None
        self._probe_lock = asyncio.Lock()

    async def _session_get(self) -> aiohttp.ClientSession:
        # Telegram API must bypass Binance SOCKS proxy (trust_env breaks getUpdates).
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def _authorized(self, chat_id: int, user_id: int | None) -> bool:
        # Any chat/group/channel where the bot is a member may use /signal.
        if chat_id != 0:
            return True
        if user_id is not None and user_id in self._allowed_user_ids:
            return True
        return False

    async def _send(self, chat_id: int, text: str) -> None:
        broadcaster = TelegramBroadcaster(self._token, str(chat_id))
        try:
            await broadcaster.send_html(text)
        finally:
            await broadcaster.close()

    async def _handle_autotune(self, chat_id: int) -> None:
        if self._probe_lock.locked():
            await self._send(chat_id, "⏳ Другой probe уже выполняется — подожди.")
            return
        async with self._probe_lock:
            await self._send(chat_id, "⏳ <b>/autotune</b> — reconcile + backtest + calibrate…")
            loop = asyncio.get_running_loop()
            try:
                report = await loop.run_in_executor(None, run_autotune)
                await self._send(chat_id, format_autotune_html(report))
            except Exception:
                LOG.exception("hunt_autotune_cmd_failed")
                await self._send(chat_id, "⚠️ /autotune failed — см. логи hunt")

    async def _handle_stats(self, chat_id: int) -> None:
        if self._probe_lock.locked():
            await self._send(chat_id, "⏳ Другой probe уже выполняется — подожди.")
            return
        async with self._probe_lock:
            await self._send(chat_id, "⏳ <b>/stats</b> — собираю метрики…")
            broadcaster = TelegramBroadcaster(self._token, str(chat_id))
            try:
                await deliver_stats_report(broadcaster)
            except Exception:
                LOG.exception("hunt_stats_cmd_failed")
                await self._send(chat_id, "⚠️ /stats failed — см. логи hunt")
            finally:
                await broadcaster.close()

    async def _handle_signals(self, chat_id: int) -> None:
        if self._probe_lock.locked():
            await self._send(chat_id, "⏳ Другой probe уже выполняется — подожди.")
            return
        async with self._probe_lock:
            await self._send(chat_id, "⏳ <b>/signals</b> — пересчитываю активные позиции…")
            broadcaster = TelegramBroadcaster(self._token, str(chat_id))
            try:
                await deliver_signals_report(broadcaster)
            except Exception:
                LOG.exception("hunt_signals_cmd_failed")
                await self._send(chat_id, "⚠️ /signals failed — см. логи hunt")
            finally:
                await broadcaster.close()

    async def _handle_signal(self, chat_id: int, parts: list[str]) -> None:
        if not parts:
            await self._send(
                chat_id,
                "Использование: <code>/signal BEATUSDT</code> или <code>/signal BEAT</code>",
            )
            return
        sym = normalize_symbol(parts[0])
        if not sym:
            await self._send(chat_id, "⚠️ Укажи символ, например <code>BEATUSDT</code>")
            return
        if self._probe_lock.locked():
            await self._send(chat_id, "⏳ Другой /signal уже выполняется — подожди.")
            return
        async with self._probe_lock:
            broadcaster = TelegramBroadcaster(self._token, str(chat_id))
            try:
                await deliver_signal_probe(broadcaster, sym)
            except Exception:
                LOG.exception("hunt_signal_cmd_failed", symbol=sym)
                await self._send(chat_id, f"⚠️ /signal {sym} failed — см. логи hunt")
            finally:
                await broadcaster.close()

    async def _handle_prepstats(self, chat_id: int) -> None:
        summary = summarize_prep_shadows()
        await self._send(chat_id, format_prep_shadow_html(summary))

    async def _handle_command(self, chat_id: int, text: str) -> None:
        parts = text.strip().split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        if cmd in {"/signal", "/sig"}:
            await self._handle_signal(chat_id, args)
        elif cmd in {"/signals", "/active"}:
            await self._handle_signals(chat_id)
        elif cmd in {"/stats", "/stat"}:
            await self._handle_stats(chat_id)
        elif cmd in {"/autotune", "/tune"}:
            await self._handle_autotune(chat_id)
        elif cmd in {"/prepstats", "/prep"}:
            await self._handle_prepstats(chat_id)
        elif cmd in {"/help", "/start"}:
            await self._send(
                chat_id,
                "<b>Hunt commands</b>\n"
                "<code>/signal BTC</code> или просто <code>BTC</code> — анализ монеты\n"
                "<code>/signals</code> — пересчёт всех active в tracker (PnL, гейты, почему блок)\n"
                "<code>/stats</code> — WR, phase matrix, TG воронка, regime, confidence\n"
                "<code>/prepstats</code> — shadow prep/start: direction WR, MFE, paper PnL\n"
                "<code>/autotune</code> — reconcile + calibrate (tiered guardrails, 1×/6h)\n"
                "· confirm → полный сигнал\n"
                "· нет confirm → сценарий + что ждём + watchlist\n"
                "· отмена сигнала → follow-up с причиной на русском",
            )

    def _extract_incoming(self, update: dict[str, Any]) -> tuple[int, int | None, str] | None:
        """message (group/DM) or channel_post (channel admin posts)."""
        for key in ("message", "channel_post", "edited_message", "edited_channel_post"):
            message = update.get(key)
            if not isinstance(message, dict):
                continue
            text = str(message.get("text") or message.get("caption") or "").strip()
            if not text:
                continue
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            chat_id = int(chat.get("id") or 0)
            from_user = message.get("from") if isinstance(message.get("from"), dict) else {}
            user_id = int(from_user.get("id") or 0) or None
            return chat_id, user_id, text
        return None

    async def _ensure_polling_mode(self) -> None:
        """getUpdates fails while a webhook is active — clear it once at startup."""
        url = _API.format(token=self._token, method="deleteWebhook")
        session = await self._session_get()
        try:
            async with session.get(url, params={"drop_pending_updates": "false"}) as resp:
                data = await resp.json(content_type=None)
            if isinstance(data, dict) and data.get("ok"):
                LOG.info("hunt_tg_webhook_cleared")
        except defensive_exc_types(Exception):
            LOG.debug("hunt_tg_webhook_clear_failed", exc_info=True)

    async def _poll_once(self) -> None:
        params: dict[str, Any] = {
            "timeout": self._poll_timeout,
            "allowed_updates": [
                "message",
                "channel_post",
                "edited_message",
                "edited_channel_post",
            ],
        }
        if self._offset is not None:
            params["offset"] = self._offset
        url = _API.format(token=self._token, method="getUpdates")
        session = await self._session_get()
        async with session.get(url, params=params) as resp:
            data = await resp.json(content_type=None)
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else None
            LOG.warning("hunt_tg_poll_not_ok", description=desc)
            await asyncio.sleep(2.0)
            return
        for update in data.get("result") or []:
            if not isinstance(update, dict):
                continue
            self._offset = int(update.get("update_id", 0)) + 1
            parsed = self._extract_incoming(update)
            if parsed is None:
                continue
            chat_id, user_id, text = parsed
            if not self._authorized(chat_id, user_id):
                LOG.warning("hunt_tg_cmd_denied", chat_id=chat_id, user_id=user_id)
                continue
            if text.startswith("/"):
                LOG.info("hunt_tg_cmd", chat_id=chat_id, user_id=user_id, text=text[:80])
                await self._handle_command(chat_id, text)
                continue
            sym = parse_symbol_text(text)
            if sym and _SYMBOL_RE.match(sym):
                LOG.info("hunt_tg_symbol_text", chat_id=chat_id, symbol=sym)
                await self._handle_signal(chat_id, [sym.replace("USDT", "")])

    async def run_forever(self) -> None:
        logging.getLogger("aiohttp").setLevel(logging.WARNING)
        await self._ensure_polling_mode()
        LOG.info("hunt_telegram_commands_started")
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except defensive_exc_types(Exception):
                LOG.debug("hunt_tg_poll_error", exc_info=True)
                await asyncio.sleep(3.0)
            except DEFENSIVE_EXC:
                LOG.debug("hunt_tg_poll_error", exc_info=True)
                await asyncio.sleep(3.0)


def build_hunt_telegram_commands(settings: Any) -> HuntTelegramCommands | None:
    token = settings.tg_token
    if not token:
        return None
    secrets = load_secrets()
    chat_ids: set[int] = set()
    for raw_chat in (settings.target_chat_id, secrets.target_chat_id):
        if not raw_chat:
            continue
        try:
            chat_ids.add(int(raw_chat))
        except (TypeError, ValueError):
            continue
    user_ids = {int(x) for x in (secrets.operator_user_ids or ())}
    return HuntTelegramCommands(
        token,
        allowed_chat_ids=frozenset(chat_ids),
        allowed_user_ids=frozenset(user_ids),
    )

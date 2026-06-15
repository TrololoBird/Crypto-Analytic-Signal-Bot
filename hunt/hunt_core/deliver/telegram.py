from __future__ import annotations


import asyncio
import hashlib
import html
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, Protocol, TypeVar, cast

import aiohttp
import structlog

from hunt_core.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# Legacy Telegram sender retained for callers that still depend on this module.
# New runtime delivery code lives under bot/telegram/.
try:
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.types import BufferedInputFile

    try:
        from aiogram.exceptions import TelegramAPIError as _AiogramAPIError
        from aiogram.exceptions import TelegramRetryAfter as _TelegramRetryAfter

        AiogramAPIError: Any = _AiogramAPIError
        TelegramRetryAfter: Any = _TelegramRetryAfter
    except ImportError:
        from aiogram import exceptions as aiogram_exceptions

        AiogramAPIError = getattr(aiogram_exceptions, "TelegramAPIError", Exception)
        TelegramRetryAfter = getattr(aiogram_exceptions, "TelegramRetryAfter", None)
    _HAS_AIogram = True
except ImportError:
    _HAS_AIogram = False
    BufferedInputFile = None  # type: ignore[misc, assignment]
    TelegramRetryAfter = None

# tenacity for retries
try:
    from tenacity import (
        before_sleep_log,
        retry,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential,
    )

    HAS_TENACITY = True
except ImportError:
    HAS_TENACITY = False


LOG = structlog.get_logger("bot.messaging")
RETRY_LOG = logging.getLogger("bot.messaging")
P = ParamSpec("P")
R = TypeVar("R")
NETWORK_RETRIES = 3
RETRY_DELAY_SECONDS = 1.5
TELEGRAM_DUPLICATE_WINDOW_SECONDS = 180
TELEGRAM_TEXT_LIMIT = 4000
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_LOG_PREVIEW_LIMIT = 500
TELEGRAM_TAGS = re.compile(r"</?(?:b|i|code|pre|a)[^>]*>", flags=re.IGNORECASE)
TELEGRAM_CHUNK_LIMIT = 3900
__all__ = (
    "DeliveryResult",
    "DisabledBroadcaster",
    "MessageBroadcaster",
    "TelegramBroadcaster",
    "WebhookBroadcaster",
    "build_message_broadcaster",
)


# Fallback retry decorator for when tenacity is not installed
def _simple_retry(
    max_attempts: int = 3, exceptions: tuple[type[Exception], ...] = (Exception,)
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Simple retry decorator as fallback when tenacity is not available."""

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts - 1:
                        wait_time = RETRY_DELAY_SECONDS * (2**attempt)  # Exponential backoff
                        LOG.debug(
                            "retry %s/%s after %.1fs: %s",
                            attempt + 1,
                            max_attempts,
                            wait_time,
                            exc,
                        )
                        await asyncio.sleep(wait_time)
            raise last_exc or RuntimeError("Retry failed")

        return wrapper

    return decorator


def _buffered_input_file_class() -> Any:
    if BufferedInputFile is None:
        msg = "BufferedInputFile is unavailable"
        raise RuntimeError(msg)
    return BufferedInputFile


def _extract_retry_after_seconds(description: str) -> int | None:
    match = re.search(r"retry after\s+(\d+)", str(description or ""), flags=re.IGNORECASE)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except TypeError, ValueError:
        return None
    return value if value > 0 else None


def _telegram_rate_limit_wait(exc: BaseException) -> int | None:
    """Seconds to wait when Telegram returns flood-control (RetryAfter / 429)."""
    if TelegramRetryAfter is not None and isinstance(exc, TelegramRetryAfter):
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            try:
                value = int(retry_after)
                if value > 0:
                    return value
            except TypeError, ValueError:
                pass
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            value = int(retry_after)
            if value > 0:
                return value
        except TypeError, ValueError:
            pass
    parsed = _extract_retry_after_seconds(str(exc))
    if parsed:
        return parsed
    name = exc.__class__.__name__.lower()
    if "retryafter" in name or "too many requests" in str(exc).lower():
        return 30
    return None


def _telegram_retryable(exc: BaseException) -> bool:
    if _telegram_rate_limit_wait(exc) is not None:
        return False
    return isinstance(exc, Exception)


def _telegram_retry() -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    if HAS_TENACITY:
        return cast(
            "Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]",
            retry(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=2, max=10),
                retry=retry_if_exception(_telegram_retryable),
                before_sleep=before_sleep_log(RETRY_LOG, logging.INFO),
                reraise=True,
            ),
        )
    return _simple_retry(3, (Exception,))


class MessageBroadcaster(Protocol):
    async def preflight_check(self) -> None: ...
    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> DeliveryResult: ...
    async def edit_html(self, message_id: int, text: str) -> None: ...
    async def send_photo(
        self,
        photo_bytes: bytes,
        caption: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: str
    message_id: int | None = None
    reason: str | None = None


class DisabledBroadcaster:
    """No-op broadcaster for runtime modes with external delivery disabled."""

    async def preflight_check(self) -> None:
        msg = "notifier provider is disabled; signal delivery is local/log only"
        raise RuntimeError(msg)

    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> DeliveryResult:
        del text, reply_to_message_id
        return DeliveryResult(status="logged", reason="notifier_disabled")

    async def edit_html(self, message_id: int, text: str) -> None:
        del message_id, text
        return

    async def send_photo(
        self,
        _photo_bytes: bytes,
        _caption: str,
        *,
        _reply_to_message_id: int | None = None,
    ) -> None:
        return None

    async def close(self) -> None:
        return None


class TelegramBroadcaster:
    duplicate_window_seconds = TELEGRAM_DUPLICATE_WINDOW_SECONDS
    min_send_interval_seconds = 1.25

    def __init__(self, token: str, target_chat_id: str) -> None:
        if not _HAS_AIogram:
            msg = "aiogram not installed. Run: pip install aiogram>=3.27.0"
            raise RuntimeError(msg)

        self.token = token
        self.target_chat_id = target_chat_id
        session = AiohttpSession()
        self.bot = Bot(token=token, session=session)
        self._send_lock = asyncio.Lock()
        self._failure_count = 0
        self._circuit_state = "closed"
        self._circuit_reset_time: datetime | None = None
        self._recent_message_hashes: dict[str, datetime] = {}
        self._send_buffer: deque[str] = deque(maxlen=50)
        self._rate_limit_until: datetime | None = None
        self._last_send_monotonic: float = 0.0

    async def preflight_check(self) -> None:
        """Verify bot token and chat access."""
        try:
            bot_info = await self.bot.get_me()
            LOG.info("telegram bot info", username=bot_info.username, id=bot_info.id)

            chat = await self.bot.get_chat(self.target_chat_id)
            LOG.info("telegram chat access confirmed", chat_id=chat.id, type=chat.type)
        except Exception as exc:
            msg = f"telegram preflight failed: {exc}"
            raise RuntimeError(msg) from exc

    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None, no_split: bool = False
    ) -> DeliveryResult:
        async with self._send_lock:
            await self._respect_rate_limit()
            if self._circuit_state == "open":
                if (
                    self._circuit_reset_time is not None
                    and datetime.now(UTC) < self._circuit_reset_time
                ):
                    self._send_buffer.append(text)
                    LOG.debug(
                        "telegram circuit breaker open; buffering message (%s buffered)",
                        len(self._send_buffer),
                    )
                    return DeliveryResult(status="buffered_circuit_open", reason="circuit_open")
                self._circuit_state = "half_open"
            self._prune_recent_hashes()
            message_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if message_hash in self._recent_message_hashes:
                LOG.debug("suppressing duplicate telegram message within dedupe window")
                return DeliveryResult(
                    status="suppressed_duplicate", reason="duplicate_within_window"
                )
            try:
                if no_split and len(text) > TELEGRAM_CHUNK_LIMIT:
                    text = text[: TELEGRAM_CHUNK_LIMIT - 12].rstrip() + "\n…"
                parts = [text] if no_split else _split_telegram_text(text)
                sent_message_id: int | None = None
                for idx, part in enumerate(parts):
                    if len(parts) > 1:
                        marker = f"<i>📄 {idx + 1}/{len(parts)}</i>\n\n"
                        part = marker + part
                    part_hash = (
                        message_hash if len(parts) == 1 else f"{message_hash}:{idx}"
                    )
                    sent_message_id = await self._send_immediate(
                        part,
                        message_hash=part_hash,
                        reply_to_message_id=(
                            reply_to_message_id if idx == 0 else None
                        ),
                    )
            except DEFENSIVE_EXC as exc:
                return DeliveryResult(status="failed", reason=f"{exc.__class__.__name__}: {exc}")
            while self._send_buffer:
                buffered = self._send_buffer.popleft()
                buffered_hash = hashlib.sha256(buffered.encode("utf-8")).hexdigest()
                if buffered_hash in self._recent_message_hashes:
                    continue
                try:
                    await self._send_immediate(
                        buffered, message_hash=buffered_hash, reply_to_message_id=None
                    )
                except DEFENSIVE_EXC as exc:
                    LOG.debug("telegram buffered message retry failed", error=str(exc))
                    self._send_buffer.appendleft(buffered)
                    break
            return DeliveryResult(status="sent", message_id=sent_message_id)

    async def edit_html(self, message_id: int, text: str) -> None:
        async with self._send_lock:
            rate_retries = 0
            max_rate_retries = 4
            while True:
                await self._respect_rate_limit()
                if self._circuit_state == "open":
                    if (
                        self._circuit_reset_time is not None
                        and datetime.now(UTC) < self._circuit_reset_time
                    ):
                        LOG.debug(
                            "telegram circuit breaker open; skipping edit for message_id=%s",
                            message_id,
                        )
                        return
                    self._circuit_state = "half_open"
                try:
                    await self._edit_immediate(message_id, text)
                except DEFENSIVE_EXC as exc:
                    self._record_send_failure(exc)
                    raise
                except Exception as exc:
                    wait = _telegram_rate_limit_wait(exc)
                    if wait is not None and rate_retries < max_rate_retries:
                        rate_retries += 1
                        self._rate_limit_until = datetime.now(UTC) + timedelta(seconds=wait)
                        LOG.warning(
                            "telegram edit flood control; waiting %ss | attempt=%d/%d id=%s",
                            wait + 1,
                            rate_retries,
                            max_rate_retries,
                            message_id,
                        )
                        await asyncio.sleep(wait + 1)
                        continue
                    if wait is not None:
                        LOG.warning(
                            "telegram edit flood control exhausted; skipping edit | message_id=%s",
                            message_id,
                        )
                        return
                    raise
                else:
                    self._failure_count = 0
                    self._circuit_state = "closed"
                    self._circuit_reset_time = None
                    return

    async def send_photo(
        self,
        photo_bytes: bytes,
        caption: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Send photo using aiogram Bot with BufferedInputFile."""
        async with self._send_lock:
            await self._respect_rate_limit()
            html_caption, plain_caption = self._prepare_captions(caption)

            try:
                BufferedInputFile = _buffered_input_file_class()
                photo_file = BufferedInputFile(photo_bytes, filename="chart.png")
                await self._respect_min_send_interval()
                await self.bot.send_photo(
                    chat_id=self.target_chat_id,
                    photo=photo_file,
                    caption=html_caption,
                    parse_mode="HTML",
                    reply_to_message_id=reply_to_message_id,
                )
                self._mark_send_timestamp()
            except DEFENSIVE_EXC as exc:
                error_str = str(exc).lower()
                # Try plain text fallback if HTML parsing failed
                if "parse" in error_str or "html" in error_str or "caption" in error_str:
                    fallback_caption = self._plain_text_fallback(caption, exc) or plain_caption
                    try:
                        BufferedInputFile = _buffered_input_file_class()
                        photo_file = BufferedInputFile(photo_bytes, filename="chart.png")
                        await self._respect_min_send_interval()
                        await self.bot.send_photo(
                            chat_id=self.target_chat_id,
                            photo=photo_file,
                            caption=fallback_caption,
                            reply_to_message_id=reply_to_message_id,
                        )
                        self._mark_send_timestamp()
                    except DEFENSIVE_EXC:
                        LOG.exception("telegram photo send failed (fallback)")
                        raise
                else:
                    LOG.exception("telegram photo send failed")
                    raise

    def _prune_recent_hashes(self) -> None:
        now = datetime.now(UTC)
        self._recent_message_hashes = {
            key: sent_at
            for key, sent_at in self._recent_message_hashes.items()
            if (now - sent_at).total_seconds() < type(self).duplicate_window_seconds
        }

    @_telegram_retry()
    async def _send_immediate(
        self,
        text: str,
        *,
        message_hash: str,
        reply_to_message_id: int | None,
    ) -> int | None:
        """Send message using aiogram Bot."""
        try:
            await self._respect_min_send_interval()
            result = await self.bot.send_message(
                chat_id=self.target_chat_id,
                text=text,
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True,
            )
            self._record_send_success(message_hash)
            LOG.info("telegram message sent", chars=len(text), preview=_message_preview(text))
        except DEFENSIVE_EXC as exc:
            error_str = str(exc).lower()
            if (
                "too long" in error_str
                or "text is too long" in error_str
                or "parse" in error_str
                or "html" in error_str
                or "tag" in error_str
            ):
                plain_text = self._plain_text_fallback(text, exc)
                if plain_text:
                    try:
                        await self._respect_min_send_interval()
                        result = await self.bot.send_message(
                            chat_id=self.target_chat_id,
                            text=plain_text,
                            reply_to_message_id=reply_to_message_id,
                            disable_web_page_preview=True,
                        )
                        self._record_send_success(message_hash)
                        LOG.info("telegram message sent (plain text)", chars=len(plain_text))
                    except DEFENSIVE_EXC as fallback_exc:
                        self._record_send_failure(fallback_exc)
                        raise
                    else:
                        return result.message_id
            self._record_send_failure(exc)
            raise
        else:
            return result.message_id

    @_telegram_retry()
    async def _edit_immediate(self, message_id: int, text: str) -> None:
        """Edit message using aiogram Bot."""
        try:
            await self._respect_min_send_interval()
            await self.bot.edit_message_text(
                chat_id=self.target_chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except DEFENSIVE_EXC as exc:
            error_str = str(exc).lower()
            # Message not modified is OK
            if "not modified" in error_str or "message is not modified" in error_str:
                return
            # Try plain text fallback
            if "parse" in error_str or "html" in error_str:
                plain_text = self._plain_text_fallback(text, exc)
                if plain_text:
                    try:
                        await self._respect_min_send_interval()
                        await self.bot.edit_message_text(
                            chat_id=self.target_chat_id,
                            message_id=message_id,
                            text=plain_text,
                            disable_web_page_preview=True,
                        )
                    except DEFENSIVE_EXC as fallback_exc:
                        if "not modified" in str(fallback_exc).lower():
                            return
                        raise
                    else:
                        return
            raise

    def _mark_send_timestamp(self) -> None:
        self._last_send_monotonic = time.monotonic()

    def _build_payload(
        self,
        text: str,
        *,
        parse_mode: str | None,
        reply_to_message_id: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": self.target_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        return payload

    def _record_send_success(self, message_hash: str) -> None:
        self._failure_count = 0
        self._circuit_state = "closed"
        self._circuit_reset_time = None
        self._rate_limit_until = None
        self._recent_message_hashes[message_hash] = datetime.now(UTC)
        self._mark_send_timestamp()

    def _record_send_failure(self, exc: Exception) -> None:
        retry_after = _telegram_rate_limit_wait(exc)
        if retry_after:
            self._rate_limit_until = datetime.now(UTC) + timedelta(seconds=retry_after)
            LOG.warning("telegram rate limited; pausing sends", seconds=retry_after)
            return

        self._failure_count += 1
        LOG.error("telegram send failed", attempt=f"{self._failure_count}/5", error=str(exc))

        if self._circuit_state == "half_open" or self._failure_count >= 5:
            self._circuit_state = "open"
            self._circuit_reset_time = datetime.now(UTC) + timedelta(minutes=5)
            LOG.critical("telegram circuit breaker opened for 5 minutes")

    async def _respect_rate_limit(self) -> None:
        if self._rate_limit_until is None:
            return
        remaining = (self._rate_limit_until - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            self._rate_limit_until = None
            return
        LOG.info("telegram send throttled by retry_after | sleep=%.1fs", remaining)
        await asyncio.sleep(remaining)
        self._rate_limit_until = None

    async def _respect_min_send_interval(self) -> None:
        interval = max(0.0, float(type(self).min_send_interval_seconds))
        if interval <= 0.0 or self._last_send_monotonic <= 0.0:
            return
        elapsed = time.monotonic() - self._last_send_monotonic
        delay = interval - elapsed
        if delay <= 0.0:
            return
        LOG.debug("telegram send paced", sleep_seconds=round(delay, 3))
        await asyncio.sleep(delay)

    @staticmethod
    def _plain_text_fallback(text: str, exc: Exception | None = None) -> str | None:
        """Convert HTML to plain text when Telegram rejects HTML parsing."""
        # Check if exception indicates recoverable HTML error
        if exc is not None:
            error_str = str(exc).lower()
            recoverable_fragments = (
                "can't parse entities",
                "unsupported start tag",
                "can't find end tag",
                "message is too long",
                "text is too long",
                "caption",
                "html",
            )
            if not any(fragment in error_str for fragment in recoverable_fragments):
                return None

        # Normalize HTML to plain text
        normalized = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        normalized = re.sub(r"</p\s*>", "\n\n", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"<[^>]+>", "", normalized)
        normalized = html.unescape(normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()

        if not normalized:
            return None
        if len(normalized) > TELEGRAM_TEXT_LIMIT:
            normalized = normalized[: TELEGRAM_TEXT_LIMIT - 1].rstrip() + "…"
        return normalized

    @staticmethod
    def _prepare_captions(text: str) -> tuple[str, str]:
        """Prepare HTML and plain text versions of caption."""
        html_caption = text.strip()
        if len(html_caption) <= TELEGRAM_CAPTION_LIMIT:
            # Generate plain fallback for safety
            plain_caption = (
                TelegramBroadcaster._plain_text_fallback(html_caption, None) or html_caption
            )
            if len(plain_caption) > TELEGRAM_CAPTION_LIMIT:
                plain_caption = plain_caption[: TELEGRAM_CAPTION_LIMIT - 1].rstrip() + "…"
            return html_caption, plain_caption

        # For oversized captions, convert to plain text
        plain_caption = TelegramBroadcaster._plain_text_fallback(html_caption, None) or html_caption
        if len(plain_caption) > TELEGRAM_CAPTION_LIMIT:
            plain_caption = plain_caption[: TELEGRAM_CAPTION_LIMIT - 1].rstrip() + "…"
        return plain_caption, plain_caption

    async def close(self) -> None:
        """Close aiogram bot session."""
        if self.bot:
            await self.bot.session.close()
            LOG.info("telegram bot session closed")


def _split_telegram_text(text: str, *, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Split HTML message into Telegram-safe chunks (paragraph-aware + hard split)."""
    if len(text) <= limit:
        return [text]

    def _hard_split(block: str) -> list[str]:
        if len(block) <= limit:
            return [block] if block else []
        out: list[str] = []
        start = 0
        while start < len(block):
            end = min(start + limit, len(block))
            if end < len(block):
                nl = block.rfind("\n", start, end)
                if nl > start + limit // 3:
                    end = nl
            piece = block[start:end].strip()
            if piece:
                out.append(piece)
            start = end if end > start else end + 1
        return out

    parts: list[str] = []
    chunk = ""
    for block in text.split("\n\n"):
        candidate = f"{chunk}\n\n{block}".strip() if chunk else block
        if len(candidate) <= limit:
            chunk = candidate
            continue
        if chunk:
            parts.extend(_hard_split(chunk))
            chunk = ""
        if len(block) <= limit:
            chunk = block
        else:
            parts.extend(_hard_split(block))
    if chunk:
        parts.extend(_hard_split(chunk))
    return parts or _hard_split(text[:limit])


def _message_preview(text: str, *, limit: int = TELEGRAM_LOG_PREVIEW_LIMIT) -> str:
    preview = " | ".join(part.strip() for part in text.splitlines() if part.strip())
    if len(preview) <= limit:
        return preview
    return preview[: limit - 1].rstrip() + "…"


def _html_to_plain_text(text: str) -> str:
    stripped = TELEGRAM_TAGS.sub("", text or "")
    stripped = re.sub(r"<[^>]+>", "", stripped)
    return html.unescape(stripped).strip()


class WebhookBroadcaster:
    def __init__(
        self,
        *,
        provider: str,
        webhook_url: str,
        username: str | None = None,
        bearer_token: str | None = None,
        include_html: bool = True,
    ) -> None:
        self.provider = provider
        self.webhook_url = webhook_url
        self.username = username
        self.bearer_token = bearer_token
        self.include_html = include_html
        self._session = aiohttp.ClientSession()

    async def preflight_check(self) -> None:
        if not self.webhook_url:
            msg = f"{self.provider} webhook_url is required"
            raise RuntimeError(msg)
        LOG.info("webhook broadcaster configured", provider=self.provider)

    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> DeliveryResult:
        del reply_to_message_id
        plain_text = _html_to_plain_text(text)
        payload = self._build_payload(text, plain_text)
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        try:
            async with self._session.post(
                self.webhook_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status >= 400:
                    body = await response.text()
                    LOG.error(
                        "webhook delivery failed | provider=%s status=%s body=%s",
                        self.provider,
                        response.status,
                        body[:200],
                    )
                    return DeliveryResult(status="failed", reason=f"http_{response.status}")
        except DEFENSIVE_EXC as exc:
            LOG.exception("webhook delivery failed | provider=%s", self.provider)
            return DeliveryResult(status="failed", reason=f"{exc.__class__.__name__}: {exc}")
        return DeliveryResult(status="sent")

    async def edit_html(self, message_id: int, text: str) -> None:
        del message_id, text
        LOG.debug("webhook broadcaster does not support edits | provider=%s", self.provider)

    async def send_photo(
        self,
        photo_bytes: bytes,
        caption: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        del photo_bytes
        await self.send_html(caption, reply_to_message_id=reply_to_message_id)

    async def close(self) -> None:
        await self._session.close()

    def _build_payload(self, html_text: str, plain_text: str) -> dict[str, Any]:
        if self.provider == "slack":
            return {"text": plain_text}
        if self.provider == "discord":
            payload: dict[str, Any] = {"content": plain_text}
            if self.username:
                payload["username"] = self.username
            return payload
        payload = {"text": plain_text}
        if self.include_html:
            payload["html"] = html_text
        if self.username:
            payload["username"] = self.username
        return payload


def build_message_broadcaster(settings: Any) -> MessageBroadcaster:
    provider = str(
        getattr(getattr(settings, "notifiers", None), "provider", "telegram") or "telegram"
    ).lower()
    if provider == "none":
        return DisabledBroadcaster()
    if provider == "telegram":
        return TelegramBroadcaster(settings.tg_token, settings.target_chat_id)

    provider_config = getattr(settings.notifiers, provider, None)
    if provider_config is None or not getattr(provider_config, "webhook_url", None):
        msg = f"notifier provider {provider!r} requires notifiers.{provider}.webhook_url"
        raise RuntimeError(msg)

    return WebhookBroadcaster(
        provider=provider,
        webhook_url=str(provider_config.webhook_url),
        username=getattr(provider_config, "username", None),
        bearer_token=getattr(provider_config, "bearer_token", None),
        include_html=bool(getattr(provider_config, "include_html", True)),
    )

# --- merged from deliver/telegram (formatters) ---

import html
from typing import Any

from hunt_core.track.tracker import duration_minutes
from hunt_core.track.pump_history import format_history_telegram

_PHASE_HUMAN: dict[str, str] = {
    "dump_active": "Активный дамп",
    "dump_initiating": "Начало дампа",
    "dump_imminent": "Дамп неизбежен",
    "dump_setup_forming": "Формируется шорт",
    "dump_confirmed": "Шорт подтверждён",
    "exhaustion_at_high": "Истощение на хаях",
    "exhaustion_watch": "Наблюдение за истощением",
    "distribution": "Распределение",
    "impulse_initiating": "Начало импульса",
    "breakout_arming": "Вооружение пробоя",
    "post_dump_bounce": "Отскок после дампа",
    "accumulation": "Накопление",
    "accumulation_watch": "Наблюдение за накоплением",
    "long_imminent": "Лонг неизбежен",
    "long_setup_forming": "Формируется лонг",
    "long_confirmed": "Лонг подтверждён",
    "no_setup": "Нет сетапа",
    "no_dump_yet": "Нет дампа",
    "no_long_yet": "Нет лонга",
}


def _squeeze_direction(row: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Infer probable breakout direction. Returns (emoji, label, evidence_lines)."""
    sq = row.get("squeeze") or {}
    lifecycle = row.get("lifecycle") or {}
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}

    bear = 0
    bull = 0
    evidence: list[str] = []

    bias = str(lifecycle.get("recommended_bias") or "")
    lc_phase = str(lifecycle.get("phase") or "")
    phase_txt = phase_human(lc_phase) if lc_phase else ""
    if bias == "short":
        bear += 2
        evidence.append(f"Lifecycle: {html.escape(phase_txt)} (медвежий)")
    elif bias == "long":
        bull += 2
        evidence.append(f"Lifecycle: {html.escape(phase_txt)} (бычий)")
    elif phase_txt:
        evidence.append(f"Lifecycle: {html.escape(phase_txt)}")

    dump_score = float(dump.get("dump_score") or 0)
    long_score = float(long_setup.get("long_score") or 0)
    if dump_score > long_score + 10:
        bear += 1
        evidence.append(f"Score шорт {dump_score:.0f} > лонг {long_score:.0f}")
    elif long_score > dump_score + 10:
        bull += 1
        evidence.append(f"Score лонг {long_score:.0f} > шорт {dump_score:.0f}")

    try:
        oi_z = float(sq.get("oi_z") or 0)
        if oi_z < -1.2:
            bear += 1
            evidence.append(f"OI падает ({oi_z:+.2f}σ) — позиции сокращаются")
        elif oi_z > 1.2:
            bull += 1
            evidence.append(f"OI растёт ({oi_z:+.2f}σ) — накопление")
        elif abs(oi_z) > 0.3:
            evidence.append(f"OI z={oi_z:+.2f}σ (нейтрально)")
    except (TypeError, ValueError):
        pass

    try:
        fund = float(sq.get("funding_pct") or 0)
        if fund > 0.05:
            bear += 1
            evidence.append(f"Funding перегрет ({fund:.4f}%) — лонги платят")
        elif fund < -0.01:
            bull += 1
            evidence.append(f"Funding отрицательный ({fund:.4f}%) — шорты платят")
        elif abs(fund) > 0.0001:
            evidence.append(f"Funding {fund:.4f}% (нейтрально)")
    except (TypeError, ValueError):
        pass

    if bear > bull:
        return "🔴", "ВНИЗ — вероятен шорт-пробой", evidence
    if bull > bear:
        return "🟢", "ВВЕРХ — вероятен лонг-пробой", evidence
    if dump_score > long_score:
        return "🔴", "СЛАБЫЙ УКЛОН ВНИЗ (score dump>long)", evidence
    if long_score > dump_score:
        return "🟢", "СЛАБЫЙ УКЛОН ВВЕРХ (score long>dump)", evidence
    return "⚪", "НЕЙТРАЛЬНО — ждать closed-bar confirm", evidence


def squeeze_trade_direction(row: dict[str, Any]) -> str:
    """short | long for unified advisory cooldown on squeeze alerts."""
    sq = row.get("squeeze") or {}
    lifecycle = row.get("lifecycle") or {}
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    bias = str(lifecycle.get("recommended_bias") or "")
    if bias in {"short", "long"}:
        return bias
    dump_score = float(dump.get("dump_score") or 0)
    long_score = float(long_setup.get("long_score") or 0)
    if dump_score > long_score + 5:
        return "short"
    if long_score > dump_score + 5:
        return "long"
    try:
        oi_z = float(sq.get("oi_z") or 0)
        if oi_z < -0.8:
            return "short"
        if oi_z > 0.8:
            return "long"
    except (TypeError, ValueError):
        pass
    return "short" if dump_score >= long_score else "long"


def format_squeeze_telegram(row: dict[str, Any]) -> str:
    sym = html.escape(str(row["symbol"]).replace("USDT", "-USDT"))
    sq = row.get("squeeze") or {}
    vol = row.get("vol_24h_m")
    vol_str = f"{vol:.0f}M" if vol is not None else "—"

    don = sq.get("donchian_width_pct_1h")
    compression_str = f"{don:.1f}%" if don is not None else "—"

    dir_emoji, dir_label, evidence = _squeeze_direction(row)
    evidence_txt = "\n".join(f"   · {e}" for e in evidence) if evidence else "   · нет сигналов"

    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    res = dump.get("resistance_liq") or dump.get("support_break_level")
    sup = long_setup.get("support_zone") or dump.get("support_break_level")
    level_parts: list[str] = []
    if res:
        level_parts.append(f"Сопротивление <code>{fmt_price(float(res))}</code>")
    if sup and sup != res:
        level_parts.append(f"Поддержка <code>{fmt_price(float(sup))}</code>")
    levels_line = "  |  ".join(level_parts) if level_parts else ""

    lines = [
        f"⚡ <b>СЖАТИЕ ЗАРЯЖЕНО · {sym}</b>",
        f"Волатильность сжата до {compression_str} от диапазона — ожидается сильный пробой. Объём 24h: <code>{vol_str}</code>",
        "",
        f"{dir_emoji} <b>Вероятное направление: {dir_label}</b>",
        evidence_txt,
    ]
    if levels_line:
        lines += ["", f"📍 {levels_line}"]
    lines += ["", "<i>Watch-only — вход только по confirmed-сигналу системы.</i>"]
    return "\n".join(lines)


def fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    if abs(v) >= 100:
        return f"{v:.3f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    if abs(v) >= 0.01:
        return f"{v:.5f}"
    return f"{v:.6f}"


def phase_human(phase: str) -> str:
    return _PHASE_HUMAN.get(phase, phase)


def phase_badge(phase: str, confirmed: bool, *, direction: str = "short") -> str:
    if confirmed:
        return "🚨"
    if direction == "long":
        return {
            "long_imminent": "🟢",
            "long_setup_forming": "🟡",
            "long_confirmed": "🚨",
            "accumulation_watch": "🔵",
            "no_long_yet": "⚪",
        }.get(phase, "⚪")
    return {
        "dump_imminent": "🔴",
        "dump_setup_forming": "🟠",
        "dump_confirmed": "🚨",
        "exhaustion_watch": "🟡",
        "no_dump_yet": "⚪",
    }.get(phase, "⚪")


def format_setup_lines(
    row: dict[str, Any],
    setup: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any],
    pos: dict[str, Any],
    price: float,
) -> list[str]:
    score_key = "dump_score" if direction == "short" else "long_score"
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    phase = str(setup.get("phase") or "—")
    confirmed = bool(setup.get("confirmed"))
    badge = phase_badge(phase, confirmed, direction=direction)

    def _opt_num(val: Any, *, digits: int = 4) -> str:
        if val is None:
            return "—"
        try:
            return f"{float(val):.{digits}f}"
        except (TypeError, ValueError):
            return "—"

    from hunt_core.deliver.dispatch import readiness_short_ru

    fuel_val = setup.get(fuel_key)
    score_val = setup.get(score_key)
    readiness = (
        readiness_short_ru(float(fuel_val))
        if fuel_val is not None
        else "—"
    )
    score = _opt_num(score_val) if score_val is not None else "—"
    dir_label = "SHORT" if direction == "short" else "LONG"

    def _rsi(key: str) -> str:
        val = (tf.get(key) or {}).get("rsi14")
        return "—" if val is None else f"{val:.0f}"

    div_bits: list[str] = []
    if direction == "short":
        if (tf.get("1h") or {}).get("bearish_rsi_div"):
            div_bits.append("bear1h✓")
        if (tf.get("4h") or {}).get("bearish_rsi_div"):
            div_bits.append("bear4h✓")
    else:
        if (tf.get("1h") or {}).get("bullish_rsi_div"):
            div_bits.append("bull1h✓")
        if (tf.get("4h") or {}).get("bullish_rsi_div"):
            div_bits.append("bull4h✓")
    div_txt = " · " + " ".join(div_bits) if div_bits else ""

    triggers = setup.get("triggers") or []
    trig_txt = html.escape(", ".join(str(t) for t in triggers[:5]))
    if len(triggers) > 5:
        trig_txt += "…"

    ez = setup.get("entry_zone") or [price, price]

    oi = pos.get("oi")
    oi_chg = pos.get("oi_chg_5m")
    fund = pos.get("funding_pct")
    taker = pos.get("taker_5m")
    ls = pos.get("ls_5m")

    if direction == "short":
        fib1272 = setup.get("fib_1272") or setup.get("resistance_liq")
        level_line = (
            f"Support <code>{fmt_price(setup.get('support_break_level'))}</code> · "
            f"fib1272 <code>{fmt_price(fib1272)}</code> · impulse H "
            f"<code>{fmt_price(row.get('impulse_high'))}</code>"
        )
    else:
        level_line = (
            f"Resistance <code>{fmt_price(setup.get('resistance_break_level'))}</code> · support "
            f"<code>{fmt_price(setup.get('support_zone'))}</code> · impulse L "
            f"<code>{fmt_price(row.get('impulse_low'))}</code>"
        )

    lines = [
        f"{badge} <b>{dir_label}</b> · <code>{phase}</code> · "
        f"{readiness} · score триггеров <code>{score}</code>",
        level_line,
        (
            f"Entry <code>{fmt_price(ez[0])}-{fmt_price(ez[1])}</code> · "
            f"SL <code>{fmt_price(setup.get('stop_loss'))}</code> · "
            f"TP1 <code>{fmt_price(setup.get('tp1'))}</code> · "
            f"TP2 <code>{fmt_price(setup.get('tp2'))}</code>"
            + (
                f" · {_rr_display(setup.get('risk_reward'))}"
                if setup.get("risk_reward")
                else ""
            )
        ),
        (
            f"RSI 1m/5m/15m/1h/4h: "
            f"<code>{_rsi('1m')}/{_rsi('5m')}/{_rsi('15m')}/{_rsi('1h')}/{_rsi('4h')}</code>"
            f"{div_txt}"
        ),
        (
            f"OI <code>{fmt_price(oi if oi is not None else None)}</code> · "
            f"Δ5m <code>{_opt_num(oi_chg)}</code> · "
            f"fund <code>{_opt_num(fund, digits=3)}%</code> · "
            f"taker5m <code>{_opt_num(taker)}</code> · "
            f"L/S <code>{_opt_num(ls)}</code>"
        ),
        f"Triggers: <code>{trig_txt or '—'}</code>",
    ]
    if confirmed:
        hard = setup.get("confirm_hard") or []
        lines.append(f"<b>✅ CONFIRM</b> {html.escape(', '.join(str(x) for x in hard))}")
    return lines


def _pct_str(a: float, b: float, direction: str) -> str:
    if a <= 0 or b <= 0:
        return ""
    if direction == "short":
        pct = (a - b) / a * 100.0
    else:
        pct = (b - a) / a * 100.0
    return f"+{pct:.1f}%"


def _market_from_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("market") or row.get("positioning") or {}
    return raw if isinstance(raw, dict) else {}


def _rr_emoji(risk_reward: Any) -> str:
    try:
        rr = float(risk_reward)
    except (TypeError, ValueError):
        return "⚠️"
    return "✅" if rr >= 3.0 else "⚠️"


def _rr_display(risk_reward: Any) -> str:
    if risk_reward is None:
        return ""
    try:
        rr = float(risk_reward)
    except (TypeError, ValueError):
        return ""
    return f"{_rr_emoji(rr)} R:R <code>{rr:.2f}</code>"


def _entry_mid(entry_zone: list[Any] | tuple[Any, ...], price: float) -> float:
    if len(entry_zone) >= 2:
        try:
            lo = float(entry_zone[0])
            hi = float(entry_zone[1])
            if lo > 0 and hi > 0:
                return (lo + hi) / 2.0
        except (TypeError, ValueError):
            pass
    return float(price or 0)


def _sl_risk_pct(entry_mid: float, stop_loss: Any, *, direction: str) -> float | None:
    if entry_mid <= 0 or stop_loss is None:
        return None
    try:
        sl = float(stop_loss)
    except (TypeError, ValueError):
        return None
    if sl <= 0:
        return None
    if direction == "short":
        return (sl - entry_mid) / entry_mid * 100.0
    return (entry_mid - sl) / entry_mid * 100.0


def _humanize_trigger(raw: str) -> str:
    ts = str(raw)
    if "volume" in ts or "vol" in ts:
        return "аномальный объём"
    if "support" in ts or "break" in ts:
        return "пробой поддержки"
    if "resistance" in ts:
        return "пробой сопротивления"
    if "cascade" in ts or "liq" in ts:
        return "каскад ликвидаций"
    if "rejection" in ts:
        return "отбой от уровня"
    if "rsi" in ts or "div" in ts:
        return "RSI-дивергенция"
    if "funding" in ts:
        return "перегрев фандинга"
    if "oi" in ts:
        return "аномалия OI"
    if "whale" in ts:
        return "крупный продавец"
    return ts.replace("_", " ").split(":")[0]


def _catalyst_label(setup: dict[str, Any], confirm_reasons: list[str]) -> str:
    for raw in confirm_reasons[:2]:
        label = _humanize_trigger(str(raw))
        if label:
            return label
    triggers = setup.get("triggers") or []
    for raw in triggers[:3]:
        label = _humanize_trigger(str(raw))
        if label:
            return label
    return "confirm-сигнал"


def _format_poc_context_line(row: dict[str, Any]) -> str:
    regime = row.get("regime") or {}
    poc = regime.get("poc_1h")
    if poc is None:
        return ""
    vah = regime.get("vah_1h")
    val = regime.get("val_1h")
    poc_dir = str(regime.get("poc_direction_1h") or "")
    dir_ru = {"long": "↑ long", "short": "↓ short"}.get(poc_dir, "→ neutral")
    parts = [
        f"POC <code>{fmt_price(float(poc))}</code>",
    ]
    if vah is not None:
        parts.append(f"VAH <code>{fmt_price(float(vah))}</code>")
    if val is not None:
        parts.append(f"VAL <code>{fmt_price(float(val))}</code>")
    parts.append(f"exit <code>{html.escape(dir_ru)}</code>")
    return "📊 " + " · ".join(parts)


def _format_liq_magnet_line(
    row: dict[str, Any], *, direction: str, price: float
) -> str:
    if price <= 0:
        return ""
    market = _market_from_row(row)
    if direction == "short":
        magnet = market.get("liq_heatmap_nearest_long")
        label = "long-liq ↓"
    else:
        magnet = market.get("liq_heatmap_nearest_short")
        label = "short-liq ↑"
    if magnet is None:
        return ""
    try:
        px = float(magnet)
    except (TypeError, ValueError):
        return ""
    if px <= 0:
        return ""
    dist = abs(px - price) / price * 100.0
    return (
        f"🧲 Liq magnet ({label}): <code>{fmt_price(px)}</code> "
        f"({dist:.1f}% от цены)"
    )


_WALL_MIN_NOTIONAL_USD = 5_000.0
_WALL_MAX_DIST_PCT = 2.0


def _best_wall_within_pct(
    levels: list[Any],
    *,
    price: float,
    side: str,
) -> dict[str, Any] | None:
    if price <= 0 or not levels:
        return None
    best: dict[str, Any] | None = None
    for lvl in levels:
        if isinstance(lvl, dict):
            px_raw = lvl.get("price")
            notional = lvl.get("notional_usd")
            if px_raw is None:
                continue
            px = float(px_raw)
            if notional is None and lvl.get("qty") is not None:
                notional = px * float(lvl["qty"])
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            px = float(lvl[0])
            notional = px * float(lvl[1])
        else:
            continue
        dist = abs(px - price) / price * 100.0
        if dist > _WALL_MAX_DIST_PCT:
            continue
        if side == "bid" and px > price:
            continue
        if side == "ask" and px < price:
            continue
        n_usd = float(notional or 0)
        if n_usd < _WALL_MIN_NOTIONAL_USD:
            continue
        if best is None or n_usd > float(best.get("notional_usd") or 0):
            best = {"price": px, "notional_usd": n_usd}
    return best


def _format_walls_context_line(row: dict[str, Any], *, price: float) -> str:
    if price <= 0:
        return ""
    cx = row.get("cross_microstructure") or {}
    walls = cx.get("book_walls") or row.get("book_walls") or {}
    if not isinstance(walls, dict):
        return ""
    bid = _best_wall_within_pct(walls.get("bid_levels") or [], price=price, side="bid")
    ask = _best_wall_within_pct(walls.get("ask_levels") or [], price=price, side="ask")
    parts: list[str] = []
    if bid:
        parts.append(
            f"Bid <code>{fmt_price(float(bid['price']))}</code> "
            f"(${float(bid['notional_usd']) / 1e3:.0f}k)"
        )
    if ask:
        parts.append(
            f"Ask <code>{fmt_price(float(ask['price']))}</code> "
            f"(${float(ask['notional_usd']) / 1e3:.0f}k)"
        )
    if not parts:
        return ""
    return "🧱 Стены ≤2%: " + " · ".join(parts)


def _format_structured_thesis(
    setup: dict[str, Any],
    *,
    direction: str,
    lc_phase: str,
    confirm_reasons: list[str],
    entry_mid: float,
) -> tuple[list[str], str]:
    """Structured thesis lines + raw triggers block for Telegram HTML."""
    phase_txt = phase_human(lc_phase) if lc_phase and lc_phase != "—" else phase_human(
        str(setup.get("phase") or "")
    )
    catalyst = _catalyst_label(setup, confirm_reasons)
    hard = confirm_reasons or [str(x) for x in (setup.get("confirm_hard") or [])]
    confluence_n = len(hard)

    risk_bits: list[str] = []
    sl_pct = _sl_risk_pct(entry_mid, setup.get("stop_loss"), direction=direction)
    if sl_pct is not None and sl_pct > 0:
        risk_bits.append(f"SL −{sl_pct:.1f}%")
    rr_txt = _rr_display(setup.get("risk_reward"))
    if rr_txt:
        risk_bits.append(rr_txt)
    risk_line = " · ".join(risk_bits) if risk_bits else "—"

    lines = [
        "💡 <b>ТЕЗИС</b>",
        f"· Фаза: {html.escape(phase_txt)} — {html.escape(catalyst)}",
        f"· Confluence: <code>{confluence_n}</code> confirm",
        f"· Риск: {risk_line}",
    ]

    raw_triggers = hard or [str(t) for t in (setup.get("triggers") or [])]
    raw_block = ""
    if raw_triggers:
        raw_txt = html.escape(", ".join(str(t) for t in raw_triggers[:8]))
        if len(raw_triggers) > 8:
            raw_txt += "…"
        raw_block = f"<pre>{raw_txt}</pre>"
    return lines, raw_block


def _reason_human(setup: dict[str, Any], *, direction: str, lc_phase: str) -> str:
    phase_txt = phase_human(lc_phase) if lc_phase and lc_phase != "—" else phase_human(
        str(setup.get("phase") or "")
    )
    triggers = setup.get("triggers") or []
    trig_short: list[str] = []
    for t in triggers[:3]:
        ts = str(t)
        if "volume" in ts or "vol" in ts:
            trig_short.append("аномальный объём")
        elif "support" in ts or "break" in ts:
            trig_short.append("пробой поддержки")
        elif "resistance" in ts:
            trig_short.append("пробой сопротивления")
        elif "cascade" in ts or "liq" in ts:
            trig_short.append("каскад ликвидаций")
        elif "rejection" in ts:
            trig_short.append("отбой от уровня")
        elif "rsi" in ts or "div" in ts:
            trig_short.append("RSI-дивергенция")
        elif "funding" in ts:
            trig_short.append("перегрев фандинга")
        elif "oi" in ts:
            trig_short.append("аномалия OI")
        elif "whale" in ts:
            trig_short.append("крупный продавец")
        else:
            trig_short.append(ts.replace("_", " ").split(":")[0])
    trig_txt = ", ".join(dict.fromkeys(trig_short))
    if phase_txt and trig_txt:
        return f"{phase_txt} · {trig_txt}"
    return phase_txt or trig_txt or "—"


def format_entry_telegram(
    row: dict[str, Any],
    *,
    direction: str,
    confirm_reasons: list[str],
    delivery_tier: str = "triggered",
) -> str:
    sym = html.escape(str(row["symbol"]).replace("USDT", "-USDT"))
    setup = row["dump"] if direction == "short" else row["long"]
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    score_key = "dump_score" if direction == "short" else "long_score"
    price = float(row.get("price") or 0)
    lc = row.get("lifecycle") or {}
    lc_phase = str(lc.get("phase") or "—")

    badge = "🔴" if direction == "short" else "🟢"
    dir_label = "SHORT" if direction == "short" else "LONG"

    from hunt_core.deliver.dispatch import readiness_label_for_setup, readiness_tier

    fuel_val = setup.get(fuel_key)
    score_val = setup.get(score_key)
    fuel = float(fuel_val) if fuel_val is not None else 0.0
    readiness = (
        readiness_label_for_setup(setup, direction=direction, row=row)
        if fuel_val is not None
        else "—"
    )
    score_str = f"{float(score_val):.0f}" if score_val is not None else "—"

    _strong_phases = frozenset(
        {
            "dump_active",
            "exhaustion_at_high",
            "distribution",
            "dump_confirmed",
            "accumulation",
            "impulse_initiating",
            "breakout_arming",
            "long_confirmed",
        }
    )
    ez = setup.get("entry_zone") or [price, price]
    armed = delivery_tier == "armed"

    tier = readiness_tier(fuel)
    if tier == "strong" and lc_phase in _strong_phases:
        rating = "🔥 СИЛЬНЫЙ"
    elif tier in {"strong", "ready"} and lc_phase in _strong_phases:
        rating = "✅ УВЕРЕННЫЙ"
    elif tier == "forming":
        rating = "⚠️ СРЕДНИЙ"
    else:
        rating = "📊 СЛАБЫЙ"

    lifecycle_line = html.escape(phase_human(lc_phase)) if lc_phase != "—" else "—"

    entry_lo = fmt_price(ez[0])
    entry_hi = fmt_price(ez[1])
    sl = fmt_price(setup.get("stop_loss"))
    tp1 = setup.get("tp1")
    tp2 = setup.get("tp2")
    tp1_pct = _pct_str(price, float(tp1), direction) if tp1 else ""
    tp2_pct = _pct_str(price, float(tp2), direction) if tp2 else ""
    tp1_lbl = setup.get("tp1_label") or ""
    tp2_lbl = setup.get("tp2_label") or ""
    tp1_str = (
        f"<code>{fmt_price(tp1)}</code>"
        + (f" (<b>{tp1_pct}</b>)" if tp1_pct else "")
        + (f" · {tp1_lbl}" if tp1_lbl else "")
    )
    tp2_str = (
        f"<code>{fmt_price(tp2)}</code>"
        + (f" (<b>{tp2_pct}</b>)" if tp2_pct else "")
        + (f" · {tp2_lbl}" if tp2_lbl else "")
    )

    entry_mid = _entry_mid(ez, price)
    thesis_lines, raw_triggers_block = _format_structured_thesis(
        setup,
        direction=direction,
        lc_phase=lc_phase,
        confirm_reasons=confirm_reasons,
        entry_mid=entry_mid,
    )
    rr_line = _rr_display(setup.get("risk_reward"))

    if armed:
        header = f"{badge} <b>SETUP ARMED · {sym} {dir_label}</b>  {rating}"
        px = fmt_price(price) if price > 0 else "—"
        action_line = (
            f"⏳ Цена сейчас <code>{px}</code> · limit-вход "
            f"<code>{entry_lo}–{entry_hi}</code> · жди retest / касание зоны"
        )
    else:
        header = f"{badge} <b>ВХОД ВЗЯТ · {sym} {dir_label}</b>  {rating}"
        action_line = ""

    phase_line = f"📌 {lifecycle_line}"
    entry_line = f"📍 Вход: <code>{entry_lo}–{entry_hi}</code>  |  Стоп: <code>{sl}</code>"
    if rr_line:
        entry_line += f"  |  {rr_line}"
    tp_line = f"🎯 TP1: {tp1_str}  |  TP2: {tp2_str}"
    if not armed:
        tp_line += (
            "\n📋 После TP1: зафиксируй 50% · BE+buf на остаток · "
            "trailing на бирже вручную после TP1"
        )

    context_lines: list[str] = []
    poc_line = _format_poc_context_line(row)
    if poc_line:
        context_lines.append(poc_line)
    liq_line = _format_liq_magnet_line(row, direction=direction, price=price)
    if liq_line:
        context_lines.append(liq_line)
    walls_line = _format_walls_context_line(row, price=price)
    if walls_line:
        context_lines.append(walls_line)

    score_line = f"📊 Score: <code>{score_str}</code> · {readiness}"
    footer = (
        "<i>Signal-only · ARMED = limit setup · TRIGGERED = цена в зоне · не auto-trade</i>"
        if armed
        else "<i>Signal-only · closed 5m/1m confirm · открывай сделку вручную</i>"
    )

    hist = format_history_telegram(row.get("pump_history"))
    hist_line = f"{html.escape(hist)}\n" if hist else ""

    body_parts = [header, phase_line]
    if action_line:
        body_parts.append(action_line)
    body_parts.extend([entry_line, tp_line])
    body_parts.extend(thesis_lines)
    if raw_triggers_block:
        body_parts.append(raw_triggers_block)
    body_parts.extend(context_lines)
    body_parts.extend([score_line, hist_line.rstrip(), footer])
    return "\n".join(part for part in body_parts if part)


def _duration_str(opened: str) -> str:
    minutes = duration_minutes(opened)
    if minutes is None:
        return "—"
    total_m = int(minutes)
    h, m = divmod(total_m, 60)
    if h > 0:
        return f"{h}ч {m}м"
    return f"{m}м"


def _trade_duration_line(payload: dict[str, Any]) -> str:
    raw_min = payload.get("duration_min")
    if raw_min is not None:
        try:
            total_m = int(float(raw_min))
            h, m = divmod(total_m, 60)
            if h > 0:
                return f"{h}ч {m}м"
            return f"{m}м"
        except (TypeError, ValueError):
            pass
    opened_raw = str(payload.get("opened_at") or "")[:19].replace("T", " ")
    return _duration_str(opened_raw)


def _format_pnl_pct(pnl: Any) -> str:
    if pnl is None:
        return ""
    try:
        val = float(pnl)
    except (TypeError, ValueError):
        return ""
    sign = "+" if val >= 0 else ""
    emoji = "💰" if val > 0 else "💸" if val < 0 else "➖"
    return f"{emoji} PnL: <b>{sign}{val:.2f}%</b>"


def _pnl_pct_from_prices(
    *,
    direction: str,
    entry_lo: Any,
    entry_hi: Any,
    exit_price: Any,
) -> float | None:
    if entry_lo is None or entry_hi is None or exit_price is None:
        return None
    try:
        entry_mid = (float(entry_lo) + float(entry_hi)) / 2.0
        exit_p = float(exit_price)
    except (TypeError, ValueError):
        return None
    if entry_mid <= 0 or exit_p <= 0:
        return None
    if direction.upper() == "SHORT":
        return (entry_mid - exit_p) / entry_mid * 100.0
    return (exit_p - entry_mid) / entry_mid * 100.0


def format_followup_telegram(followup: Any, row: dict[str, Any]) -> str:
    from hunt_core.deliver.dispatch import invalidate_detail_human

    sym = html.escape(str(followup.symbol).replace("USDT", "-USDT"))
    direction = followup.direction.upper()
    price = fmt_price(followup.price)
    lc = row.get("lifecycle") or {}
    payload = followup.payload if isinstance(followup.payload, dict) else {}
    event = followup.event

    sl = fmt_price(payload.get("stop_loss"))
    tp1_lvl = fmt_price(payload.get("tp1"))
    tp2_lvl = fmt_price(payload.get("tp2"))
    entry_lo = payload.get("entry_lo")
    entry_hi = payload.get("entry_hi")
    entry_zone = (
        f"{fmt_price(entry_lo)}–{fmt_price(entry_hi)}"
        if entry_lo is not None and entry_hi is not None
        else "—"
    )
    opened_raw = str(payload.get("opened_at") or "")[:19].replace("T", " ")
    msg_id = payload.get("entry_message_id")
    entry_ref = f"Вход {entry_zone}"
    if msg_id:
        entry_ref += f" · сигнал TG <code>#{msg_id}</code>"

    reason_raw = str(payload.get("reason") or "")
    detail_human = invalidate_detail_human(str(followup.detail or ""), reason=reason_raw)

    if event == "fix_profit_tp1":
        fix_pct = int(payload.get("partial_fixed_pct") or 50)
        new_sl = fmt_price(payload.get("stop_loss"))
        pnl_line = _format_pnl_pct(payload.get("pnl_pct"))
        if not pnl_line:
            est = _pnl_pct_from_prices(
                direction=direction,
                entry_lo=entry_lo,
                entry_hi=entry_hi,
                exit_price=payload.get("tp1"),
            )
            pnl_line = _format_pnl_pct(est)
        duration = _trade_duration_line(payload)
        trade_meta = f"{pnl_line} · ⏱ {duration}" if pnl_line else f"⏱ {duration}"
        return (
            f"✅ <b>TP1 достигнут · {sym} {direction}</b>\n"
            f"{trade_meta}\n"
            f"🔒 Зафиксируй <b>{fix_pct}%</b> позиции · Стоп перенесён на безубыток <code>{new_sl}</code>\n"
            f"🎯 Следующая цель: TP2 <code>{tp2_lvl}</code>\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "fix_profit_tp2":
        duration = _duration_str(opened_raw)
        skipped = bool(payload.get("tp1_skipped"))
        extra = " (TP1 пролёт)" if skipped else ""
        return (
            f"📋 <b>Закрыт {sym} {direction}{extra}</b>\n"
            f"💰 PnL: TP2 <code>{tp2_lvl}</code> · Длит: {duration}\n"
            f"📌 Причина: Достигнут TP2\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "trailing_updated":
        new_sl = fmt_price(payload.get("stop_loss"))
        protected = payload.get("protected_pnl_pct")
        try:
            prot_str = f"+{float(protected):.1f}%"
        except (TypeError, ValueError):
            prot_str = "—"
        return (
            f"📈 <b>TRAILING АКТИВЕН · {sym} {direction}</b>\n"
            f"Стоп подтянут → <code>{new_sl}</code> · защита ~<b>{prot_str}</b>\n"
            f"⚡ На бирже вручную подтяни SL до этого уровня (Hunt не торгует).\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "entry_triggered":
        return (
            f"🎯 <b>TRIGGERED · {sym} {direction}</b>\n"
            f"✅ Цена <code>{price}</code> в зоне входа <code>{entry_zone}</code>\n"
            f"📍 Стоп: <code>{sl}</code> · TP1: <code>{tp1_lvl}</code> · TP2: <code>{tp2_lvl}</code>\n"
            f"{entry_ref}\n"
            f"<i>ARMED → TRIGGERED · limit касание · не auto-trade</i>"
        )

    if event == "invalidate":
        duration = _trade_duration_line(payload)

        _reason_map = {
            "stop_hit": ("🔴 Стоп-лосс пробит", "Позиция закрылась по стопу."),
            "trailing_stop_profit": (
                "✅ Trailing stop / фиксация",
                "Позиция закрыта по подтянутому стопу в зоне профита.",
            ),
            "tp1": ("✅ Достигнут TP1", "Взята первая цель."),
            "tp2": ("✅ Достигнут TP2", "Взята финальная цель."),
            "bounce_invalidate": (
                "🔄 Lifecycle: отскок — шорт отменён",
                "Рынок начал восстановление — тезис на дамп исчерпан.",
            ),
            "time_stall": (
                "⏳ Тезис не сработал",
                "Нет прогресса за 8ч — вероятно, сетап поглощён рынком.",
            ),
            "bias_flip": (
                "🔄 Фаза сменилась против позиции",
                "Lifecycle перешёл в противоположную фазу — продолжение маловероятно.",
            ),
            "support_lost": (
                "⚠️ Потеря поддержки",
                "Ключевая поддержка утрачена — лонг-тезис сломан.",
            ),
        }
        lc_phase_payload = str(payload.get("phase") or "")
        phase_txt = phase_human(lc_phase_payload) if lc_phase_payload else ""

        reason_title, reason_body = _reason_map.get(
            reason_raw,
            (f"📌 {html.escape(detail_human)}", ""),
        )
        if reason_raw == "lifecycle_stale" and phase_txt:
            reason_title = "🔄 Фаза сменилась против позиции"
            reason_body = f"Новая фаза: <b>{html.escape(phase_txt)}</b> — тезис исчерпан."

        # PnL from tracker payload (preferred) or entry midpoint vs exit tick
        pnl_line = _format_pnl_pct(payload.get("pnl_pct"))
        if not pnl_line:
            est = _pnl_pct_from_prices(
                direction=direction,
                entry_lo=entry_lo,
                entry_hi=entry_hi,
                exit_price=followup.price,
            )
            pnl_line = _format_pnl_pct(est)
        if pnl_line:
            pnl_line += "\n"

        action_needed = reason_raw not in {
            "stop_hit",
            "trailing_stop_profit",
            "tp1",
            "tp2",
        }
        action_line = "⚡ <b>Закрой позицию вручную</b>\n" if action_needed else ""

        if reason_raw in {"trailing_stop_profit", "tp1", "tp2"}:
            verdict = "✅ Профит"
        elif reason_raw in {"stop_hit"}:
            verdict = "🔴 Стоп"
        elif reason_raw in {"time_stall", "timeout"}:
            verdict = "⏳ Таймаут"
        else:
            verdict = "🔄 Тезис снят"

        return (
            f"📋 <b>ПОЗИЦИЯ ЗАКРЫТА · {sym} {direction}</b>\n"
            f"<b>{verdict}</b> · {reason_title}\n"
            f"{reason_body}\n"
            f"{action_line}"
            f"{pnl_line}"
            f"⏱ В сделке: {duration}\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "stop_warning":
        return (
            f"⚠️ <b>СТОП РЯДОМ · {sym} {direction}</b>\n"
            f"Цена <code>{price}</code> близко к SL <code>{sl}</code>\n"
            f"Реши: держать или фиксировать вручную.\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    badges = {"phase_change": "🔄", "avg_zone": "➕"}
    titles = {"phase_change": "PHASE CHANGE", "avg_zone": "AVG ZONE"}
    badge = badges.get(event, "📣")
    title = titles.get(event, event)
    lc_phase_now = html.escape(phase_human(str(lc.get("phase") or "—")))
    return (
        f"{badge} <b>{title}</b>\n"
        f"{sym} · <code>{direction}</code> · цена <code>{price}</code>\n"
        f"{html.escape(detail_human)}\n"
        f"{entry_ref}\n"
        f"SL <code>{sl}</code> · TP1 <code>{tp1_lvl}</code> · TP2 <code>{tp2_lvl}</code>\n"
        f"Фаза: {lc_phase_now}\n"
        f"<i>Hunt follow-up · не auto-trade</i>"
    )


def split_telegram(text: str, *, limit: int = 3900) -> list[str]:
    return _split_telegram_text(text, limit=limit)


async def send_telegram_chunks(
    broadcaster: TelegramBroadcaster,
    text: str,
    *,
    log_key: str,
    log: Any,
) -> bool:
    ok = True
    for idx, part in enumerate(split_telegram(text)):
        result = await broadcaster.send_html(part)
        if result.status != "sent":
            log.warning(
                f"{log_key}_failed",
                part=idx + 1,
                status=result.status,
                reason=result.reason,
            )
            ok = False
        else:
            log.info(f"{log_key}_sent", part=idx + 1, message_id=result.message_id)
    return ok


# ── MTF + Cross-Exchange formatters for /signal (PINNED symbols) ─────────────

def _fmt_price(v: float) -> str:
    if v >= 10_000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:.5f}"


def format_mtf_section(
    mtf: Any, *, row: dict[str, Any] | None = None, include_scenarios: bool = True
) -> str:
    """
    Format MTF structure table + two scenarios for a PINNED /signal reply.

    ``mtf`` is a ``MTFConfluence`` dataclass from ``hunt_core.analysis.deep_signal``.
    """
    from hunt_core.deliver.dispatch import geometry_block_reason

    def _geometry_blocked(direction: str) -> bool:
        if not row:
            return False
        setup = (row.get("dump") if direction == "short" else row.get("long")) or {}
        if not isinstance(setup, dict):
            return False
        return geometry_block_reason(setup, row=row, direction=direction) is not None

    _TREND_EMOJI = {"bull": "🟢", "bear": "🔴", "neutral": "🟡"}
    _TREND_RU = {"bull": "Bull", "bear": "Bear", "neutral": "Нейт"}
    _TF_NAME = {"1w": "1W ", "1d": "1D ", "4h": "4H ", "15m": "15M"}

    sym = html.escape(str(getattr(mtf, "symbol", "?")).replace("USDT", "-USDT"))
    lines: list[str] = [
        f"🔭 <b>АНАЛИЗ · {sym}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "📊 <b>МТФ СТРУКТУРА</b>",
    ]
    for tf_key in ("1w", "1d", "4h", "15m"):
        sig = (mtf.tf_signals or {}).get(tf_key)
        if sig is None:
            continue
        emoji = _TREND_EMOJI.get(sig.trend, "🟡")
        tlabel = _TREND_RU.get(sig.trend, "Нейт")
        name = _TF_NAME.get(tf_key, tf_key.upper())
        lines.append(
            f"<code>{name}</code> {emoji} {tlabel:4s} | RSI {sig.rsi14:5.1f} | {html.escape(sig.label)}"
        )

    dominant = getattr(mtf, "dominant", "neutral")
    dom_ru = {"long": "ЛОНГ", "short": "ШОРТ", "neutral": "БОКОВИК"}.get(dominant, "—")
    lines.append("")
    lines.append(f"🎯 <b>MTF bias:</b> {dom_ru}")

    lc_phase = str(((row or {}).get("lifecycle") or {}).get("phase") or "")
    if not include_scenarios or lc_phase in {"no_setup", "accumulation_watch", "exhaustion_watch"}:
        lines.append("")
        lines.append(
            "<i>⚠️ Сценарии входа скрыты — lifecycle без confirm; MTF bias справочно.</i>"
        )
        return "\n".join(lines)

    scenarios = [mtf.long_scenario, mtf.short_scenario]
    if dominant in {"long", "short"}:
        scenarios = [
            s for s in scenarios if getattr(s, "direction", "") == dominant
        ] or scenarios

    for sc in scenarios:
        dir_str = getattr(sc, "direction", "long")
        geo_block = _geometry_blocked(dir_str)
        is_main = dir_str == dominant and dominant != "neutral"
        if is_main and geo_block:
            star = " · ⚠️ watch-only (уровни)"
        elif is_main:
            star = " ★ ОСНОВНОЙ"
        else:
            star = ""
        dir_emoji = "📈" if dir_str == "long" else "📉"
        dir_ru = "ЛОНГ" if dir_str == "long" else "ШОРТ"
        score = float(getattr(sc, "score", 0))
        htf_aligned = int(getattr(sc, "htf_count", 0))
        htf_total = int(getattr(sc, "htf_total", 0))
        evidence: list[str] = list(getattr(sc, "evidence", []))

        entry_lo = float(getattr(sc, "entry_lo", 0))
        entry_hi = float(getattr(sc, "entry_hi", 0))
        tp1 = float(getattr(sc, "tp1", 0))
        tp2 = float(getattr(sc, "tp2", 0))
        stop = float(getattr(sc, "stop", 0))

        ref = entry_hi if dir_str == "long" else entry_lo
        pct1 = (tp1 - ref) / ref * 100 if ref else 0.0
        pct2 = (tp2 - ref) / ref * 100 if ref else 0.0
        stop_ref = entry_lo if dir_str == "long" else entry_hi
        stop_pct = (stop - stop_ref) / stop_ref * 100 if stop_ref else 0.0

        lines.append("")
        lines.append(
            f"{dir_emoji} <b>СЦЕНАРИЙ {dir_ru}</b>  [Score: {score:.2f}]{html.escape(star)}"
        )
        if htf_total:
            ev_str = ", ".join(evidence[1:4]) if len(evidence) > 1 else ""
            lines.append(
                f"HTF {htf_aligned}/{htf_total}"
                + (f" · {html.escape(ev_str)}" if ev_str else "")
            )
        lines.append(f"Зона входа:  <code>{_fmt_price(entry_lo)} – {_fmt_price(entry_hi)}</code>")
        lines.append(f"TP1:         <code>{_fmt_price(tp1)}</code>  ({pct1:+.1f}%)")
        lines.append(f"TP2:         <code>{_fmt_price(tp2)}</code>  ({pct2:+.1f}%)")
        lines.append(f"Стоп:        <code>{_fmt_price(stop)}</code>  ({stop_pct:+.1f}%)")

    lines.append("")
    lines.append("<i>⚠️ Watch-only — вход только по confirmed-сигналу системы.</i>")
    return "\n".join(lines)


def format_volume_profile_section(row: dict[str, Any]) -> str:
    """POC/VAH/VAL from regime + optional cross-exchange merge."""
    cx = row.get("cross_microstructure") or {}
    vp1h = cx.get("volume_profile_1h") or {}
    regime = row.get("regime") or {}
    poc = vp1h.get("poc") or regime.get("poc_1h")
    vah = vp1h.get("vah") or regime.get("vah_1h")
    val = vp1h.get("val") or regime.get("val_1h")
    if poc is None:
        return ""
    src = "cross" if vp1h.get("poc") is not None else "BNC"
    lines = [
        f"📊 <b>Volume profile 1h</b> ({src}): POC <code>{_fmt_price(float(poc))}</code>",
    ]
    if vah is not None:
        lines[-1] += f" · VAH <code>{_fmt_price(float(vah))}</code>"
    if val is not None:
        lines[-1] += f" · VAL <code>{_fmt_price(float(val))}</code>"
    vp15 = cx.get("volume_profile_15m") or {}
    if vp15.get("poc") is not None:
        lines.append(
            f"15m POC <code>{_fmt_price(float(vp15['poc']))}</code>"
            + (f" · VAH <code>{_fmt_price(float(vp15['vah']))}</code>" if vp15.get("vah") else "")
        )
    return "\n".join(lines)


def format_book_walls_section(row: dict[str, Any]) -> str:
    """Top cross-venue or single-exchange limit clusters."""
    cx = row.get("cross_microstructure") or {}
    walls = cx.get("book_walls") or row.get("book_walls") or {}
    if not isinstance(walls, dict):
        return ""
    bids = walls.get("bid_levels") or []
    asks = walls.get("ask_levels") or []
    if not bids and not asks:
        return ""

    def _wall_line(side: str, levels: list[Any], emoji: str) -> str:
        parts: list[str] = []
        for lvl in levels[:3]:
            if isinstance(lvl, dict):
                px = lvl.get("price")
                notional = lvl.get("notional_usd")
                ex = lvl.get("exchange") or walls.get("source") or "?"
            elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                px, qty = float(lvl[0]), float(lvl[1])
                notional = round(px * qty, 0)
                ex = "?"
            else:
                continue
            if px is None:
                continue
            tag = str(ex)[:3].upper()
            parts.append(f"{tag} {_fmt_price(float(px))} (${float(notional or 0)/1e3:.1f}k)")
        if not parts:
            return ""
        return f"{emoji} {side}: " + " · ".join(parts)

    lines = ["📚 <b>Стены стакана</b>"]
    venues = walls.get("venues")
    if isinstance(venues, list) and len(venues) > 1:
        lines[0] += f" <i>({len(venues)} бирж)</i>"
    bid_line = _wall_line("Bid", bids, "🟢")
    ask_line = _wall_line("Ask", asks, "🔴")
    if bid_line:
        lines.append(bid_line)
    if ask_line:
        lines.append(ask_line)
    imb = walls.get("depth_imbalance")
    if imb is not None:
        lines.append(f"Imbalance: <code>{float(imb):+.3f}</code>")
    return "\n".join(lines)


def format_cross_microstructure_section(row: dict[str, Any]) -> str:
    """Cross-exchange taker flow + liq data note."""
    cx = row.get("cross_microstructure") or {}
    if not cx:
        return ""
    lines: list[str] = []
    taker = cx.get("taker_flow") or {}
    per = taker.get("per_exchange") or {}
    if per:
        bits = [f"{ex[:3].upper()} {float(v):.2f}" for ex, v in per.items()]
        consensus = taker.get("consensus")
        tail = f" → consensus <code>{consensus:.2f}</code>" if consensus is not None else ""
        lines.append("Order flow (taker): " + " · ".join(bits) + tail)
    note = cx.get("liquidation_note")
    if note:
        lines.append(f"<i>{html.escape(str(note))}</i>")
    return "\n".join(lines)


_MICRO_TAG_RU = {
    "book_against": "стакан против",
    "book_for": "стакан за",
    "microprice_against": "микроцена против",
    "microprice_for": "микроцена за",
    "backwardation": "бэквордация",
    "contango": "контанго",
    "mixed": "смешанная",
    "bullish": "бычья",
    "bearish": "медвежья",
    "neutral": "нейтр.",
}


def _humanize_micro_bias(raw: str) -> str:
    """Turn the raw 'microstructure=mixed score=-0.35; k=v:tag · …' debug string into
    a short Russian phrase. Falls back to the raw text if the shape is unexpected."""
    if not raw or "=" not in raw:
        return html.escape(raw)
    parts: list[str] = []
    head = raw.split(";", 1)[0].strip()  # 'microstructure=mixed score=-0.35'
    label = ""
    score = ""
    for tok in head.split():
        if tok.startswith("microstructure="):
            label = _MICRO_TAG_RU.get(tok.split("=", 1)[1], tok.split("=", 1)[1])
        elif tok.startswith("score="):
            score = tok.split("=", 1)[1]
    if label:
        parts.append(f"{label}{f' ({score})' if score else ''}")
    tail = raw.split(";", 1)[1] if ";" in raw else ""
    for seg in tail.split("·"):
        seg = seg.strip()
        if ":" not in seg:
            continue
        tag = seg.rsplit(":", 1)[1].strip()
        ru = _MICRO_TAG_RU.get(tag)
        if ru and ru != "нейтр.":
            parts.append(ru)
    return html.escape(" · ".join(parts)) if parts else html.escape(raw)


def format_pinned_deep_analysis(row: dict[str, Any]) -> str:
    """Deep /signal block: MTF + micro + three-way verdict (long/short/sideways)."""
    from hunt_core.analysis.pinned_deep import PinnedVerdict, build_pinned_verdict

    verdict_raw = row.get("pinned_verdict")
    if isinstance(verdict_raw, PinnedVerdict):
        verdict = verdict_raw
    else:
        verdict = build_pinned_verdict(row)

    mtf = row.get("mtf")
    body_parts: list[str] = []
    include_scenarios = verdict.kind != "sideways" and str(
        (row.get("lifecycle") or {}).get("phase") or ""
    ) not in {"no_setup", "accumulation_watch", "exhaustion_watch"}
    if mtf is not None:
        mtf_text = format_mtf_section(
            mtf, row=row, include_scenarios=include_scenarios
        )
        if mtf_text.startswith("🔭"):
            mtf_lines = mtf_text.split("\n")
            mtf_text = "\n".join(mtf_lines[3:]).lstrip()
        if mtf_text:
            body_parts.append(mtf_text)

    panel = verdict.indicator_panel or row.get("indicator_panel")
    if panel is not None and getattr(panel, "total_votes", 0) > 0:
        lv = int(getattr(panel, "long_votes", 0))
        sv = int(getattr(panel, "short_votes", 0))
        tv = int(getattr(panel, "total_votes", 0))
        dom = str(getattr(panel, "dominant", "neutral"))
        body_parts.append(
            f"📊 <b>Консенсус индикаторов:</b> {lv}/{tv} long · {sv}/{tv} short · {dom}"
        )

    sym = html.escape(str(row.get("symbol", "?")).replace("USDT", "-USDT"))
    header = [
        f"🔭 <b>ГЛУБОКИЙ АНАЛИЗ · {sym}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if verdict.kind == "long":
        verdict_line = f"✅ <b>ВЕРДИКТ: ЛОНГ</b> (уверенность {verdict.confidence:.2f})"
    elif verdict.kind == "short":
        verdict_line = f"✅ <b>ВЕРДИКТ: ШОРТ</b> (уверенность {verdict.confidence:.2f})"
    else:
        verdict_line = "⚖️ <b>ВЕРДИКТ: БОКОВИК / НАБЛЮДЕНИЕ</b> — нет confirm для входа"

    footer = [
        "",
        verdict_line,
        html.escape(verdict.reason),
    ]
    if verdict.micro_bias:
        footer.append(f"Микроструктура: {_humanize_micro_bias(verdict.micro_bias)}")
    if verdict.cvd_note:
        footer.append(f"Order flow: <i>{html.escape(verdict.cvd_note)}</i>")

    vp_block = format_volume_profile_section(row)
    if vp_block:
        footer.extend(["", vp_block])
    walls_block = format_book_walls_section(row)
    if walls_block:
        footer.extend(["", walls_block])
    cx_micro = format_cross_microstructure_section(row)
    if cx_micro:
        footer.extend(["", cx_micro])

    from hunt_core.analysis.deep_signal import format_liquidity_scenarios_telegram
    from hunt_core.analysis.deep_signal import format_poc_level_scenarios_telegram

    poc_raw = row.get("poc_level_scenarios") or getattr(verdict, "poc_level_scenarios", None)
    if poc_raw is not None:
        poc_block = format_poc_level_scenarios_telegram(
            poc_raw.to_dict() if hasattr(poc_raw, "to_dict") else poc_raw
        )
        if poc_block:
            footer.extend(["", poc_block])

    liq_raw = row.get("liquidity_scenarios") or getattr(verdict, "liquidity_scenarios", None)
    if liq_raw is not None:
        liq_block = format_liquidity_scenarios_telegram(
            liq_raw.to_dict() if hasattr(liq_raw, "to_dict") else liq_raw
        )
        if liq_block:
            footer.extend(["", liq_block])

    market = row.get("market") or {}
    if market:
        fund = market.get("funding_pct")
        oi = market.get("oi_chg_5m")
        taker = market.get("taker_5m")
        bits: list[str] = []
        if fund is not None:
            bits.append(f"fund {float(fund):.4f}%")
        if oi is not None:
            bits.append(f"OIΔ5m {float(oi):+.2f}%")
        if taker is not None:
            bits.append(f"taker {float(taker):.2f}")
        if bits:
            footer.append("Derivatives: " + " · ".join(bits))

    if verdict.kind == "sideways":
        footer.append("")
        footer.append("<i>→ Вход не рекомендуется. HTF bias — контекст; жди готовность≥60 + confirm.</i>")

    return "\n".join(header + body_parts + footer)


def format_cross_exchange_section(cx: dict[str, Any]) -> str:
    """Format cross-exchange intel block for /signal reply."""
    if not cx:
        return ""
    funding: dict[str, Any] = cx.get("funding") or {}
    oi_usd: dict[str, Any] = cx.get("oi_usd") or {}
    mark_price: dict[str, Any] = cx.get("mark_price") or {}
    funding_spread = float(cx.get("funding_spread") or 0)
    consensus = str(cx.get("funding_consensus") or "neutral")
    oi_total = float(cx.get("oi_total") or 0)
    price_div = float(cx.get("price_divergence_pct") or 0)

    _NAMES = {"binance": "BNC", "bybit": "BYB", "okx": "OKX", "bitget": "BGT"}

    funding_parts: list[str] = []
    for ex, rate in funding.items():
        if rate is None:
            continue
        label = _NAMES.get(ex, ex.upper()[:3])
        sign = "+" if rate >= 0 else ""
        funding_parts.append(f"{label} {sign}{rate*100:.4f}%")

    price_parts: list[str] = []
    for ex, mp in mark_price.items():
        if not mp:
            continue
        label = _NAMES.get(ex, ex.upper()[:3])
        price_parts.append(f"{label} {_fmt_price(mp)}")

    listed = cx.get("listed") or {}
    listed_parts = [
        f"{_NAMES.get(ex, ex.upper()[:3])}{'✓' if ok else '✗'}"
        for ex, ok in listed.items()
    ]
    lines: list[str] = ["🌐 <b>КРОСС-БИРЖА</b> <i>(universe: Binance)</i>"]
    if listed_parts:
        lines.append("Листинг: " + " ".join(listed_parts))
    if funding_parts:
        lines.append("Funding:  " + "  |  ".join(funding_parts))
        if consensus == "divergent":
            lines.append("          ⚠️ Дивергенция — биржи не согласованы")
        elif consensus == "bull":
            lines.append("          🟢 Фандинг бычий на всех биржах")
        elif consensus == "bear":
            lines.append("          🔴 Фандинг медвежий на всех биржах")
    if oi_total > 0:
        oi_b = oi_total / 1e9
        lines.append(f"OI Total: <code>${oi_b:.2f}B</code>")
    if price_parts:
        spread_str = f"  (spread {price_div:.3f}%)" if price_div > 0 else ""
        lines.append("Цены:     " + "  |  ".join(price_parts) + html.escape(spread_str))
    return "\n".join(lines) if len(lines) > 1 else ""


def _zone_status(
    *,
    direction: str,
    price: float,
    entry_lo: float,
    entry_hi: float,
) -> str:
    if entry_lo <= 0 or entry_hi <= 0 or price <= 0:
        return ""
    if entry_lo <= price <= entry_hi:
        return "в зоне"
    if direction == "short":
        if price < entry_lo:
            return "ждём откат ↑"
        return "выше зоны"
    if price > entry_hi:
        return "ждём откат ↓"
    return "ниже зоны"


def _hunt_scenario_lines(
    setup: dict[str, Any],
    *,
    direction: str,
    row: dict[str, Any],
    price: float,
    active: bool = False,
    mtf_sc: Any | None = None,
) -> list[str]:
    """User-facing scenario from hunt detector levels + readiness (not raw MTF score)."""
    from hunt_core.deliver.dispatch import display_readiness_score, geometry_block_evidence

    emoji = "📈" if direction == "long" else "📉"
    label = "ЛОНГ" if direction == "long" else "ШОРТ"
    readiness = display_readiness_score(setup, direction=direction, row=row)
    geo = geometry_block_evidence(setup, row=row, direction=direction)
    phase = str(setup.get("phase") or "—")
    confirmed = bool(setup.get("confirmed"))
    star = " ★" if active or confirmed else ""

    ez = setup.get("entry_zone") or []
    try:
        entry_lo = float(ez[0]) if len(ez) >= 1 else 0.0
        entry_hi = float(ez[1]) if len(ez) >= 2 else entry_lo
    except (TypeError, ValueError):
        entry_lo = entry_hi = 0.0

    htf = ""
    if mtf_sc is not None and int(getattr(mtf_sc, "htf_total", 0) or 0) > 0:
        htf = (
            f" · HTF {int(getattr(mtf_sc, 'htf_count', 0))}/"
            f"{int(getattr(mtf_sc, 'htf_total', 0))}"
        )

    if entry_lo <= 0 or entry_hi <= 0:
        return [f"{emoji} <b>{label}</b> · нет валидных уровней"]

    sl = setup.get("stop_loss")
    tp1 = setup.get("tp1")
    rr = setup.get("risk_reward")
    zone = _zone_status(
        direction=direction, price=price, entry_lo=entry_lo, entry_hi=entry_hi
    )
    zone_txt = f" · <i>{zone}</i>" if zone else ""

    lines = [
        (
            f"{emoji} <b>{label}</b>{star} · готовность <code>{readiness:.0f}</code>"
            f"{htf} · <code>{html.escape(phase)}</code>"
        ),
        (
            f"Вход <code>{_fmt_price(entry_lo)}–{_fmt_price(entry_hi)}</code>{zone_txt}"
            f" → TP <code>{_fmt_price(tp1)}</code>"
            f" · SL <code>{_fmt_price(sl)}</code>"
            + (
                f" · R:R <code>{float(rr):.2f}</code>"
                if rr is not None
                else ""
            )
        ),
    ]
    geo_reason = geo.get("reason")
    if geo_reason:
        lines.append(f"⚠️ <i>{html.escape(str(geo_reason))}</i>")
    elif setup.get("levels_viable") is False:
        lines.append("⚠️ <i>уровни не прошли валидацию</i>")
    return lines


def _compact_scenario_lines(sc: Any, *, emoji: str, label: str, active: bool = False) -> list[str]:
    """3-line scenario block for user-facing /signal."""
    if sc is None:
        return [f"{emoji} <b>{label}</b> · нет данных"]
    entry_lo = float(getattr(sc, "entry_lo", 0) or 0)
    entry_hi = float(getattr(sc, "entry_hi", 0) or 0)
    tp1 = float(getattr(sc, "tp1", 0) or 0)
    stop = float(getattr(sc, "stop", 0) or 0)
    score = float(getattr(sc, "score", 0) or 0)
    htf_count = int(getattr(sc, "htf_count", 0) or 0)
    htf_total = int(getattr(sc, "htf_total", 0) or 0)
    evidence = list(getattr(sc, "evidence", []) or [])
    star = " ★" if active else ""
    htf = f" · HTF {htf_count}/{htf_total}" if htf_total else ""
    out = [
        f"{emoji} <b>{label}</b>{star} · score <code>{score:.2f}</code>{htf}",
        (
            f"Вход <code>{_fmt_price(entry_lo)}–{_fmt_price(entry_hi)}</code>"
            f" → TP <code>{_fmt_price(tp1)}</code>"
            f" · SL <code>{_fmt_price(stop)}</code>"
        ),
    ]
    if evidence:
        note = str(evidence[0])
        if len(note) > 72:
            note = note[:69] + "…"
        out.append(f"<i>{html.escape(note)}</i>")
    return out


def _setup_scenario_lines(
    setup: dict[str, Any],
    *,
    direction: str,
    price: float,
    active: bool = False,
) -> list[str]:
    """Fallback scenario when MTF object is unavailable."""
    emoji = "📈" if direction == "long" else "📉"
    label = "ЛОНГ" if direction == "long" else "ШОРТ"
    fuel = float(setup.get("long_fuel" if direction == "long" else "dump_fuel") or 0)
    phase = str(setup.get("phase") or "—")
    ez = setup.get("entry_zone") or [price, price]
    star = " ★" if active or setup.get("confirmed") else ""
    return [
        f"{emoji} <b>{label}</b>{star} · fuel <code>{fuel:.0f}</code> · <code>{html.escape(phase)}</code>",
        (
            f"Вход <code>{_fmt_price(float(ez[0]))}–{_fmt_price(float(ez[1] if len(ez) > 1 else ez[0]))}</code>"
            f" → TP <code>{_fmt_price(setup.get('tp1'))}</code>"
            f" · SL <code>{_fmt_price(setup.get('stop_loss'))}</code>"
        ),
    ]


def _brief_reason(row: dict[str, Any]) -> str:
    lc = row.get("lifecycle") or {}
    lc_phase = str(lc.get("phase") or "")
    if lc_phase == "no_setup":
        return "Нет structural setup — оба сценария справочные, вход только по confirm"

    verdict = row.get("pinned_verdict")
    if verdict is not None:
        reason = str(getattr(verdict, "reason", "") or "")
    else:
        reason = (
            f"phase={lc.get('phase') or '—'}"
            f" · bias={lc.get('recommended_bias') or '—'}"
        )
    parts = [p.strip() for p in reason.replace(";", "·").split("·") if p.strip()]
    short = " · ".join(parts[:2]) if parts else "ждём closed-bar confirm"
    return short[:160] + ("…" if len(short) > 160 else "")


def format_signal_brief_telegram(
    row: dict[str, Any],
    *,
    confirmed_direction: str | None = None,
    added_watch: bool = False,
    delivery_tier: Any = None,
) -> str:
    """User /signal reply: two scenario forecasts + one-line explanation only."""
    from hunt_core.analysis.deep_signal import probe_header

    sym = html.escape(str(row.get("symbol", "?")).replace("USDT", "-USDT"))
    price = float(row.get("price") or 0)
    lc = row.get("lifecycle") or {}
    phase = html.escape(str(lc.get("phase") or "—"))
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    mtf = row.get("mtf")
    long_mtf = getattr(mtf, "long_scenario", None) if mtf is not None else None
    short_mtf = getattr(mtf, "short_scenario", None) if mtf is not None else None

    lines: list[str] = [
        f"🔭 <b>{sym}</b> · <code>{_fmt_price(price)}</code> · <code>{phase}</code>",
    ]
    if confirmed_direction:
        dir_ru = "ШОРТ" if confirmed_direction == "short" else "ЛОНГ"
        lines.append(f"✅ <b>Confirm {dir_ru}</b>")
        if delivery_tier is not None:
            tier = getattr(delivery_tier, "tier", None) or (
                delivery_tier.get("tier") if isinstance(delivery_tier, dict) else None
            )
            if tier:
                lines.append(f"Tier: <code>{html.escape(str(tier))}</code>")
    else:
        badge, label, sub = probe_header(row)
        if sub:
            lines.append(f"{badge} <b>{html.escape(label)}</b> · <i>{html.escape(sub)}</i>")
        else:
            lines.append(f"{badge} <b>{html.escape(label)}</b>")

    lines.append("")
    lines.extend(
        _hunt_scenario_lines(
            long_setup,
            direction="long",
            row=row,
            price=price,
            active=confirmed_direction == "long",
            mtf_sc=long_mtf,
        )
    )
    lines.append("")
    lines.extend(
        _hunt_scenario_lines(
            dump,
            direction="short",
            row=row,
            price=price,
            active=confirmed_direction == "short",
            mtf_sc=short_mtf,
        )
    )

    lines.append("")
    lines.append(f"💬 {html.escape(_brief_reason(row))}")
    lines.append("<i>Watch-only · вход только по confirm системы</i>")
    if added_watch:
        bias = str(lc.get("recommended_bias") or "both")
        lines.append(f"<i>+ watchlist ({html.escape(bias)})</i>")
    return "\n".join(lines)

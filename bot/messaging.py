from __future__ import annotations

import asyncio
import html
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Awaitable, Callable, ParamSpec, Protocol, TypeVar, cast

import aiohttp
import structlog

# TODO: [Sentinel] Redundant implementation - this logic should be consolidated with bot/telegram/
# aiogram for Telegram Bot API
try:
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession

    try:
        from aiogram.exceptions import TelegramAPIError as _AiogramAPIError

        AiogramAPIError: Any = _AiogramAPIError
    except ImportError:
        from aiogram import exceptions as aiogram_exceptions

        AiogramAPIError = getattr(aiogram_exceptions, "TelegramAPIError", Exception)
    _HAS_AIogram = True
except ImportError:
    _HAS_AIogram = False

# tenacity for retries
try:
    from tenacity import (
        retry,
        stop_after_attempt,
        wait_exponential,
        retry_if_exception_type,
        before_sleep_log,
    )

    HAS_TENACITY = True
except ImportError:
    HAS_TENACITY = False


LOG = structlog.get_logger("bot.messaging")
RETRY_LOG = logging.getLogger("bot.messaging")
UTC = timezone.utc
P = ParamSpec("P")
R = TypeVar("R")
NETWORK_RETRIES = 3
RETRY_DELAY_SECONDS = 1.5
TELEGRAM_DUPLICATE_WINDOW_SECONDS = 180
TELEGRAM_TEXT_LIMIT = 4000
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_LOG_PREVIEW_LIMIT = 500


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
    try:
        from aiogram.types import BufferedInputFile
    except ImportError as exc:
        raise RuntimeError("BufferedInputFile is unavailable") from exc
    return BufferedInputFile


def _telegram_retry() -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    if HAS_TENACITY:
        return cast(
            Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]],
            retry(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=2, max=10),
                retry=retry_if_exception_type((Exception,)),
                before_sleep=before_sleep_log(RETRY_LOG, logging.WARNING),
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
        raise RuntimeError("notifier provider is disabled; signal delivery is local/log only")

    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> DeliveryResult:
        return DeliveryResult(status="logged", reason="notifier_disabled")

    async def edit_html(self, message_id: int, text: str) -> None:
        return None

    async def send_photo(
        self,
        photo_bytes: bytes,
        caption: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        return None

    async def close(self) -> None:
        return None


class TelegramBroadcaster:
    duplicate_window_seconds = TELEGRAM_DUPLICATE_WINDOW_SECONDS
    min_send_interval_seconds = 1.25

    def __init__(self, token: str, target_chat_id: str) -> None:
        if not _HAS_AIogram:
            raise RuntimeError("aiogram not installed. Run: pip install aiogram>=3.27.0")

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
            raise RuntimeError(f"telegram preflight failed: {exc}") from exc

    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
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
                sent_message_id = await self._send_immediate(
                    text,
                    message_hash=message_hash,
                    reply_to_message_id=reply_to_message_id,
                )
            except Exception as exc:
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
                except Exception:
                    self._send_buffer.appendleft(buffered)
                    break
            return DeliveryResult(status="sent", message_id=sent_message_id)

    async def edit_html(self, message_id: int, text: str) -> None:
        async with self._send_lock:
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
            except Exception as exc:
                self._record_send_failure(exc)
                raise
            self._failure_count = 0
            self._circuit_state = "closed"
            self._circuit_reset_time = None

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
            except Exception as exc:
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
                    except Exception as fallback_exc:
                        LOG.error("telegram photo send failed (fallback): %s", fallback_exc)
                        raise
                else:
                    LOG.error("telegram photo send failed: %s", exc)
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
            return result.message_id
        except Exception as exc:
            # Try plain text fallback if HTML parsing failed
            error_str = str(exc).lower()
            if "parse" in error_str or "html" in error_str or "tag" in error_str:
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
                        return result.message_id
                    except Exception as fallback_exc:
                        self._record_send_failure(fallback_exc)
                        raise
            self._record_send_failure(exc)
            raise

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
        except Exception as exc:
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
                        return
                    except Exception as fallback_exc:
                        if "not modified" in str(fallback_exc).lower():
                            return
                        raise
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
        # Extract retry_after from aiogram exception or use fallback
        retry_after = None
        if hasattr(exc, "retry_after"):
            retry_after = getattr(exc, "retry_after", None)
        elif hasattr(exc, "retry_after_seconds"):
            retry_after = getattr(exc, "retry_after_seconds", None)

        if retry_after:
            self._rate_limit_until = datetime.now(UTC) + timedelta(seconds=retry_after)
            LOG.warning("telegram rate limited; pausing sends", seconds=retry_after)

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


def _message_preview(text: str, *, limit: int = TELEGRAM_LOG_PREVIEW_LIMIT) -> str:
    preview = " | ".join(part.strip() for part in text.splitlines() if part.strip())
    if len(preview) <= limit:
        return preview
    return preview[: limit - 1].rstrip() + "…"


def _extract_retry_after_seconds(description: str) -> int | None:
    match = re.search(r"retry after\s+(\d+)", str(description or ""), flags=re.IGNORECASE)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _html_to_plain_text(text: str) -> str:
    stripped = re.sub(r"<[^>]+>", "", text or "")
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
            raise RuntimeError(f"{self.provider} webhook_url is required")
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
                    LOG.warning(
                        "webhook delivery failed | provider=%s status=%s body=%s",
                        self.provider,
                        response.status,
                        body[:200],
                    )
                    return DeliveryResult(status="failed", reason=f"http_{response.status}")
        except Exception as exc:
            LOG.warning("webhook delivery failed | provider=%s error=%s", self.provider, exc)
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
        raise RuntimeError(
            f"notifier provider {provider!r} requires notifiers.{provider}.webhook_url"
        )

    return WebhookBroadcaster(
        provider=provider,
        webhook_url=str(provider_config.webhook_url),
        username=getattr(provider_config, "username", None),
        bearer_token=getattr(provider_config, "bearer_token", None),
        include_html=bool(getattr(provider_config, "include_html", True)),
    )

"""Fail-fast logging guard for live smoke runs."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


# Informational threshold review — not a smoke failure.
_WARNING_ALLOWLIST = frozenset({"bot.config_audit"})


class SmokeFailFastError(RuntimeError):
    """Raised when live smoke aborts due to a logged WARNING/ERROR."""

    def __init__(self, message: str, *, record: logging.LogRecord) -> None:
        super().__init__(message)
        self.record = record


@dataclass(slots=True)
class SmokeFailFastGuard:
    """Abort live smoke when ERROR/WARNING appear during monitored phases."""

    loop: asyncio.AbstractEventLoop
    abort_event: asyncio.Event
    enabled: bool = True
    fail_on_warning: bool = True
    warning_allowlist: frozenset[str] = _WARNING_ALLOWLIST
    _startup_active: bool = True
    _aborted: bool = False
    _trigger_record: logging.LogRecord | None = None
    _handler: logging.Handler | None = None

    def mark_startup_complete(self) -> None:
        """After ``bot.start()`` — runtime WARNINGs no longer abort smoke."""
        self._startup_active = False

    def install(self) -> None:
        if not self.enabled or self._handler is not None:
            return

        guard = self

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                guard._handle_record(record)

        handler = _Handler(level=logging.WARNING)
        logging.getLogger().addHandler(handler)
        self._handler = handler

    def uninstall(self) -> None:
        if self._handler is None:
            return
        logging.getLogger().removeHandler(self._handler)
        self._handler = None

    def note_asyncio_exception(self, context: dict[str, object]) -> None:
        if not self.enabled or self._aborted:
            return
        exc = context.get("exception")
        message = str(context.get("message", "asyncio exception"))
        record = logging.LogRecord(
            name="asyncio",
            level=logging.ERROR,
            pathname=__file__,
            lineno=0,
            msg=message if exc is None else f"{message}: {exc!r}",
            args=(),
            exc_info=exc if isinstance(exc, BaseException) else None,
        )
        self._handle_record(record)

    def raise_if_aborted(self) -> None:
        record = self._trigger_record
        if record is None:
            return
        level = logging.getLevelName(record.levelno)
        raise SmokeFailFastError(
            f"live smoke fail-fast on {level} [{record.name}] {record.getMessage()}",
            record=record,
        )

    def _handle_record(self, record: logging.LogRecord) -> None:
        if not self.enabled or self._aborted:
            return
        if record.levelno >= logging.ERROR:
            self._abort(record)
            return
        if not self.fail_on_warning or record.levelno < logging.WARNING:
            return
        if not self._startup_active:
            return
        if record.name in self.warning_allowlist:
            return
        self._abort(record)

    def _abort(self, record: logging.LogRecord) -> None:
        if self._aborted:
            return
        self._aborted = True
        self._trigger_record = record
        self.loop.call_soon_threadsafe(self.abort_event.set)


async def wait_for_runtime_or_abort(
    seconds: float,
    abort_event: asyncio.Event,
) -> bool:
    """Wait up to *seconds*; return True when fail-fast fired."""
    if seconds <= 0.0:
        return abort_event.is_set()
    try:
        await asyncio.wait_for(abort_event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


def install_asyncio_exception_logging(
    *,
    guard: SmokeFailFastGuard | None = None,
    base_handler: Callable[[asyncio.AbstractEventLoop, dict[str, object]], None] | None = None,
) -> None:
    loop = asyncio.get_running_loop()

    def _log_exception(loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        if base_handler is not None:
            base_handler(loop, context)
        else:
            msg = context.get("exception", context.get("message"))
            logging.getLogger("asyncio").exception(
                "Unhandled asyncio exception: %s",
                msg,
                exc_info=msg if isinstance(msg, BaseException) else None,
            )
        if guard is not None:
            guard.note_asyncio_exception(context)

    loop.set_exception_handler(_log_exception)

"""Hot-path latency telemetry helpers for п.41."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

LOG = logging.getLogger("bot.telemetry")

F = TypeVar("F", bound=Callable[..., Any])

_SLOW_THRESHOLD_MS = 100.0


def timed(name: str | None = None, *, threshold_ms: float = _SLOW_THRESHOLD_MS) -> Callable[[F], F]:
    """Decorator that logs execution time when it exceeds *threshold_ms*."""
    def decorator(func: F) -> F:
        label = name or func.__qualname__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            try:
                return func(*args, **kwargs)
            finally:
                elapsed_ms = (time.monotonic() - start) * 1000.0
                if elapsed_ms >= threshold_ms:
                    LOG.warning("SLOW %s | %.2fms", label, elapsed_ms)

        return wrapper  # type: ignore[return-value]
    return decorator

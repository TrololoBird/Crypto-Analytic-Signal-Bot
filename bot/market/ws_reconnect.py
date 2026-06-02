"""Reconnect/backoff helpers extracted from ``bot.ws_manager``."""

from __future__ import annotations

import logging
import random
from typing import Any

from websockets import exceptions as ws_exceptions

LOG = logging.getLogger("bot.ws_manager")

_BACKOFF_RESET_AFTER_SECONDS = 90.0


def compute_disconnect_delay(
    manager: Any,
    *,
    endpoint: str,
    url: str,
    exc: Exception,
    elapsed: float,
    delay: float,
) -> float:
    """Update streak/reconnect metadata and return next retry delay."""
    error_text = str(exc).lower()
    keepalive_timeout = "keepalive ping timeout" in error_text
    if elapsed < _BACKOFF_RESET_AFTER_SECONDS and not keepalive_timeout:
        manager._short_lived_streak += 1
    else:
        manager._short_lived_streak = 0

    close_detail = ""
    if isinstance(exc, ws_exceptions.ConnectionClosed):
        close_code = exc.rcvd.code if exc.rcvd else "not_received"
        close_reason = repr(exc.rcvd.reason) if exc.rcvd else ""
        close_detail = f" code={close_code} reason={close_reason}"

    if keepalive_timeout:
        min_delay = 1.0
    elif manager._short_lived_streak >= 8:
        min_delay = 300.0
    elif manager._short_lived_streak >= 5:
        min_delay = 30.0
    elif manager._short_lived_streak >= 3:
        min_delay = 5.0
    else:
        min_delay = 1.0

    next_delay = min(300.0, max(1.0, delay, min_delay))
    next_delay = min(
        300.0,
        next_delay + random.uniform(0.0, min(0.5, next_delay * 0.1)),
    )  # seconds: reconnect backoff stays between 1s and 5min.

    manager._last_reconnect_reason = f"{endpoint}:{exc}"
    manager._last_reconnect_reason_by_endpoint[endpoint] = str(exc)
    level = logging.INFO
    log_reason = "keepalive_ping_timeout" if keepalive_timeout else str(exc)
    LOG.log(
        level,
        (
            "ws disconnected | endpoint=%s url=%s reason=%s close_detail=%s "
            "uptime=%.1fs retry_in=%.1fs streak=%d"
        ),
        endpoint,
        url,
        log_reason,
        close_detail,
        elapsed,
        next_delay,
        manager._short_lived_streak,
    )
    return next_delay

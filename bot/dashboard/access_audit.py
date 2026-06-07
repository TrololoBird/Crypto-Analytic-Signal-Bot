"""Dashboard HTTP rate limiting and access audit logging."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

LOG = logging.getLogger("bot.dashboard.access_audit")


def client_ip_from_request(request: Any) -> str:
    forwarded = str(request.headers.get("X-Forwarded-For", "") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return str(host or "unknown")


class DashboardAccessAuditor:
    """Sliding-window per-IP rate limiter with append-only access log."""

    def __init__(
        self,
        *,
        limit_per_minute: int,
        log_path: Path | None,
        enabled: bool = True,
    ) -> None:
        self._limit = max(1, int(limit_per_minute))
        self._log_path = log_path
        self._enabled = bool(enabled)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check_rate_limit(self, client_ip: str) -> bool:
        """Return True when request is allowed."""
        now = time.monotonic()
        with self._lock:
            window = self._hits.setdefault(client_ip, deque())
            while window and now - window[0] > 60.0:
                window.popleft()
            if len(window) >= self._limit:
                return False
            window.append(now)
            return True

    def record_access(
        self,
        *,
        client_ip: str,
        method: str,
        path: str,
        status_code: int,
        blocked: bool = False,
    ) -> None:
        if not self._enabled or self._log_path is None:
            return
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "client_ip": client_ip,
            "method": method,
            "path": path,
            "status_code": int(status_code),
            "blocked": bool(blocked),
        }
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
        except OSError:
            LOG.debug("dashboard access audit write failed", exc_info=True)

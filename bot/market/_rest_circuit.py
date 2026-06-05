"""REST circuit breaker helpers (extracted from rest_impl.py)."""

from __future__ import annotations

import logging
import time

LOG = logging.getLogger("bot.market.rest")


class RestCircuitMixin:
    """Circuit breaker state and guards mixed into RestHttpMixin."""

    _circuit_failures: dict[str, int]
    _circuit_open_until: dict[str, float]
    _circuit_half_open: set[str]
    _circuit_failure_threshold: int
    _circuit_open_duration_seconds: float
    _critical_operations: set[str]

    def _is_circuit_open(self, operation: str) -> bool:
        open_until = self._circuit_open_until.get(operation, 0.0)
        now = time.monotonic()
        if now < open_until:
            return True
        if open_until > 0.0:
            if operation in self._circuit_half_open:
                return True
            self._circuit_half_open.add(operation)
            return False
        return False

    def _record_circuit_failure(self, operation: str) -> None:
        if operation in self._circuit_half_open:
            self._circuit_half_open.discard(operation)
            self._circuit_open_until[operation] = (
                time.monotonic() + self._circuit_open_duration_seconds
            )
            self._circuit_failures[operation] = 0
            LOG.error(
                "circuit breaker half-open probe failed | operation=%s duration=%.0fs",
                operation,
                self._circuit_open_duration_seconds,
            )
            return
        failures = self._circuit_failures.get(operation, 0) + 1
        self._circuit_failures[operation] = failures
        threshold = self._circuit_failure_threshold
        if operation not in self._critical_operations:
            threshold = self._circuit_failure_threshold * 5
        if failures >= threshold:
            open_until = time.monotonic() + self._circuit_open_duration_seconds
            self._circuit_open_until[operation] = open_until
            LOG.error(
                "circuit breaker opened | operation=%s failures=%d threshold=%d duration=%.0fs",
                operation,
                failures,
                threshold,
                self._circuit_open_duration_seconds,
            )
            self._circuit_failures[operation] = 0

    def _record_circuit_success(self, operation: str) -> None:
        self._circuit_half_open.discard(operation)
        self._circuit_open_until.pop(operation, None)
        if operation in self._circuit_failures:
            del self._circuit_failures[operation]

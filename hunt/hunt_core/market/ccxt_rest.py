"""Hunt CCXT REST gate — weight pacing + Binance 418/429 handling (engine-aligned).

Hunt cannot hot-swap CCXT clients mid-tick (concurrent gather). On IP-ban we only
schedule proxy rotation for the next cycle boundary.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

import ccxt

from hunt_core.market.rate_limit import (
    REST_WEIGHT_PACE_LIMIT,
    SlidingWindowRateLimiter,
    WeightBudgetManager,
)
from hunt_core.market.capacity import (
    BINANCE_FAPI_DATA_PACE_5M,
    secondary_limit_for,
)
from hunt_core.market.ccxt_guard import CcxtGuard, is_ccxt_ip_ban, is_ccxt_rate_limited

LOG = logging.getLogger("hunt_core.market.ccxt_rest")

T = TypeVar("T")

_HEADER_WEIGHT_KEYS = (
    "x-mbx-used-weight-1m",
    "X-MBX-USED-WEIGHT-1M",
    "x-mbx-used-weight",
    "X-MBX-USED-WEIGHT",
)


def _header_used_weight(headers: Any) -> int | None:
    if not headers:
        return None
    if isinstance(headers, dict):
        for key in _HEADER_WEIGHT_KEYS:
            raw = headers.get(key)
            if raw is None:
                continue
            try:
                return max(0, int(float(raw)))
            except (TypeError, ValueError):
                continue
    return None


@dataclass
class HuntCcxtRestGate:
    """Pace REST weight and record 418/429 pauses — no mid-tick proxy swap."""

    guard: CcxtGuard = field(default_factory=CcxtGuard)
    weight_budget: WeightBudgetManager = field(
        default_factory=lambda: WeightBudgetManager(
            max_weight=REST_WEIGHT_PACE_LIMIT,
            window_seconds=60.0,
        )
    )
    fapi_budget: SlidingWindowRateLimiter = field(
        default_factory=lambda: SlidingWindowRateLimiter(
            max_requests=BINANCE_FAPI_DATA_PACE_5M,
            window_seconds=300.0,
        )
    )
    _secondary_budgets: dict[str, SlidingWindowRateLimiter] = field(
        default_factory=dict,
        repr=False,
    )
    pending_proxy_failover: bool = False

    def _secondary_budget(self, exchange: str) -> SlidingWindowRateLimiter:
        key = exchange.lower()
        lim = self._secondary_budgets.get(key)
        if lim is None:
            max_req, window = secondary_limit_for(key)
            lim = SlidingWindowRateLimiter(
                max_requests=max_req,
                window_seconds=window,
            )
            self._secondary_budgets[key] = lim
        return lim

    async def acquire_fapi(self, *, label: str) -> None:
        await self.await_pause()
        await self.fapi_budget.acquire(label=f"fapi:{label}")

    async def acquire_secondary(self, exchange: str, *, label: str) -> None:
        await self.await_pause()
        await self._secondary_budget(exchange).acquire(
            label=f"{exchange}:{label}",
        )

    async def acquire_binance_weight(self, *, weight: int, label: str) -> None:
        """Pace uncategorized Binance calls (direct ``_ex.fetch_*`` without invoke)."""
        await self.await_pause()
        await self.weight_budget.acquire(weight=max(1, int(weight)), label=label)

    async def await_pause(self, *, cap_s: float = 120.0) -> float:
        remaining = self.guard.remaining_pause_s()
        if remaining <= 0:
            return 0.0
        sleep_s = min(remaining, cap_s)
        LOG.info(
            "hunt_ccxt_rate_pause | remaining_s=%.0f sleep_s=%.0f",
            remaining,
            sleep_s,
        )
        await asyncio.sleep(sleep_s)
        return sleep_s

    def sync_weight_from_exchange(self, exchange: Any) -> None:
        headers = getattr(exchange, "last_response_headers", None)
        used = _header_used_weight(headers)
        if used is not None:
            self.weight_budget.force_floor(used)

    async def invoke_fapi(
        self,
        exchange: Any,
        factory: Callable[[], Any],
        *,
        context: str,
    ) -> T:
        await self.await_pause()
        await self.fapi_budget.acquire(label=f"fapi:{context}")
        try:
            result = factory()
            if asyncio.iscoroutine(result):
                result = await result
        except ccxt.BaseError as exc:
            self.record_error(exc, context=context)
            raise
        self.sync_weight_from_exchange(exchange)
        return result  # type: ignore[return-value]

    async def invoke(
        self,
        exchange: Any,
        factory: Callable[[], Any],
        *,
        context: str,
        weight: int = 5,
    ) -> T:
        await self.await_pause()
        await self.weight_budget.acquire(weight=max(1, int(weight)), label=context)
        try:
            result = factory()
            if asyncio.iscoroutine(result):
                result = await result
        except ccxt.BaseError as exc:
            self.record_error(exc, context=context)
            raise
        self.sync_weight_from_exchange(exchange)
        return result  # type: ignore[return-value]

    async def invoke_secondary(
        self,
        exchange_name: str,
        exchange: Any,
        factory: Callable[[], Any],
        *,
        context: str,
    ) -> T:
        await self.await_pause()
        await self._secondary_budget(exchange_name).acquire(
            label=f"{exchange_name}:{context}",
        )
        try:
            result = factory()
            if asyncio.iscoroutine(result):
                result = await result
        except ccxt.BaseError as exc:
            self.record_error(exc, context=f"{exchange_name}:{context}")
            raise
        return result  # type: ignore[return-value]

    def record_error(self, exc: BaseException, *, context: str) -> None:
        if not is_ccxt_rate_limited(exc) and not is_ccxt_ip_ban(exc):
            return
        kind = self.guard.record(exc, context=context)
        pause_s = self.guard.pause_seconds(exc)
        self.guard.extend_pause(pause_s)
        if kind == "ip_ban":
            LOG.critical(
                "hunt_binance_ip_ban | context=%s pause_s=%.0f error=%s",
                context,
                pause_s,
                exc,
            )
            self.pending_proxy_failover = True
        elif kind == "rate_limit":
            LOG.info(
                "hunt_ccxt_rate_limit | context=%s pause_s=%.0f error=%s",
                context,
                pause_s,
                exc,
            )

    def schedule_proxy_failover(self) -> None:
        self.pending_proxy_failover = True

    def consume_proxy_failover_flag(self) -> bool:
        if not self.pending_proxy_failover:
            return False
        self.pending_proxy_failover = False
        return True


__all__ = ["HuntCcxtRestGate"]

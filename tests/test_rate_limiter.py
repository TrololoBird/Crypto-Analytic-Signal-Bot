from __future__ import annotations

import asyncio
import time

from bot.market_data import _SlidingWindowRateLimiter, _WeightBudgetManager


def run(coro):
    return asyncio.run(coro)


def test_weight_budget_tracks_used_weight() -> None:
    async def scenario() -> int:
        limiter = _WeightBudgetManager(max_weight=10, window_seconds=60.0)
        waited = await limiter.acquire(weight=4, label="unit")
        assert waited == 0.0
        await limiter.acquire(weight=3, label="unit")
        return limiter.used_weight

    assert run(scenario()) == 7


def test_weight_budget_ignores_zero_weight() -> None:
    async def scenario() -> int:
        limiter = _WeightBudgetManager(max_weight=10, window_seconds=60.0)
        waited = await limiter.acquire(weight=0, label="unit")
        assert waited == 0.0
        return limiter.used_weight

    assert run(scenario()) == 0


def test_weight_budget_throttles_when_limit_is_exceeded() -> None:
    async def scenario() -> float:
        limiter = _WeightBudgetManager(max_weight=1, window_seconds=0.05)
        await limiter.acquire(weight=1, label="first")
        started = time.monotonic()
        waited = await limiter.acquire(weight=1, label="second")
        elapsed = time.monotonic() - started
        assert waited > 0.0
        return elapsed

    assert run(scenario()) >= 0.04


def test_sliding_window_rate_limiter_throttles_after_capacity() -> None:
    async def scenario() -> float:
        limiter = _SlidingWindowRateLimiter(max_requests=1, window_seconds=0.05)
        await limiter.acquire(label="first")
        started = time.monotonic()
        waited = await limiter.acquire(label="second")
        elapsed = time.monotonic() - started
        assert waited > 0.0
        return elapsed

    assert run(scenario()) >= 0.04

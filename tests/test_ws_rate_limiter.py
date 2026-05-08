from __future__ import annotations

import pytest

from bot.ws_manager import RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_rejects_same_second_burst() -> None:
    limiter = RateLimiter(max_per_second=2)

    assert await limiter.acquire() is True
    assert await limiter.acquire() is True
    assert await limiter.acquire() is False

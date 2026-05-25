from __future__ import annotations

import asyncio


def test_asyncio_gather_return_exceptions_smoke_pattern() -> None:
    async def ok() -> str:
        return "ok"

    async def fail() -> str:
        raise RuntimeError("boom")

    async def scenario() -> tuple[str, BaseException]:
        first, second = await asyncio.gather(ok(), fail(), return_exceptions=True)
        assert first == "ok"
        assert isinstance(second, RuntimeError)
        return first, second

    result = asyncio.run(scenario())
    assert result[0] == "ok"

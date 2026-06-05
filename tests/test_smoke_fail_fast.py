from __future__ import annotations

import asyncio
import logging

import pytest

from scripts.smoke_fail_fast import (
    SmokeFailFastError,
    SmokeFailFastGuard,
    wait_for_runtime_or_abort,
)


@pytest.fixture
def guard_setup():
    loop = asyncio.new_event_loop()
    abort_event = asyncio.Event()
    guard = SmokeFailFastGuard(loop=loop, abort_event=abort_event, enabled=True)
    guard.install()
    yield loop, abort_event, guard
    guard.uninstall()
    loop.close()


def test_error_aborts_smoke(guard_setup) -> None:
    loop, abort_event, guard = guard_setup
    logging.getLogger("bot.runtime.bot").error("sqlite schema mismatch")
    loop.run_until_complete(abort_event.wait())
    with pytest.raises(SmokeFailFastError, match="ERROR"):
        guard.raise_if_aborted()


def test_config_audit_warning_is_allowed(guard_setup) -> None:
    _loop, abort_event, guard = guard_setup
    logging.getLogger("bot.config_audit").warning("CONFIG AUDIT: lane caps")
    assert not abort_event.is_set()
    guard.raise_if_aborted()


def test_startup_warning_aborts_smoke(guard_setup) -> None:
    loop, abort_event, guard = guard_setup
    logging.getLogger("bot.runtime.bot").warning("shortlist build degraded")
    loop.run_until_complete(abort_event.wait())
    with pytest.raises(SmokeFailFastError, match="WARNING"):
        guard.raise_if_aborted()


def test_runtime_warning_does_not_abort_after_startup(guard_setup) -> None:
    _loop, abort_event, guard = guard_setup
    guard.mark_startup_complete()
    logging.getLogger("bot.runtime.bot").warning("shortlist build degraded")
    assert not abort_event.is_set()
    guard.raise_if_aborted()


@pytest.mark.asyncio
async def test_wait_for_runtime_or_abort_returns_on_fail_fast() -> None:
    abort_event = asyncio.Event()
    abort_event.set()
    assert await wait_for_runtime_or_abort(60.0, abort_event) is True

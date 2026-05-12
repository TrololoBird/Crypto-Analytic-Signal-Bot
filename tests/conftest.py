from __future__ import annotations

import asyncio
import inspect

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    # Keep compatibility with environments that do not have pytest-asyncio
    # but still load `asyncio_mode` from pyproject.toml.
    parser.addini("asyncio_mode", "asyncio mode compatibility option", default="auto")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: mark test to run in an event loop")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    test_func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_func):
        return None
    if "asyncio" not in pyfuncitem.keywords:
        return None

    sig = inspect.signature(test_func)
    kwargs = {name: value for name, value in pyfuncitem.funcargs.items() if name in sig.parameters}

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(test_func(**kwargs))
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    return True

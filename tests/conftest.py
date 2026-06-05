"""Pytest configuration — live Binance tests run only when explicitly enabled."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    if os.environ.get("PYTEST_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="Set PYTEST_LIVE=1 to run live Binance public API tests")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)

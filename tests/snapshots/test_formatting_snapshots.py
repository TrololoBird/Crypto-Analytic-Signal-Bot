"""Snapshot tests for Telegram signal formatting.

Phase -1 (pre-refactoring safety net): freeze current formatting output
before any delivery/formatting refactors. Each test captures the full
Telegram HTML and compares with a stored snapshot.

Adding a new snapshot:
    pytest tests/snapshots/ --snapshot-update
    # then manually verify the .ambr diff in git
"""

from __future__ import annotations

import pytest

from bot.delivery.formatting import format_signal_message
from tests.fixtures.signal_factory import safe_usdt_rejected, xag_short_activated


class TestFormatSignalMessage:
    """Compact channel card (CHANNEL_SIGNAL_POLICY, the default)."""

    def test_safe_usdt_rejected(self, snapshot_scrubbed) -> None:
        """SAFE-USDT LONG with minimal score (0.15) — no tier."""
        signal = safe_usdt_rejected()
        result = format_signal_message(
            signal,
            pending_expiry_minutes=240,
        )
        assert result == snapshot_scrubbed

    def test_xag_short_activated(self, snapshot_scrubbed) -> None:
        """XAG-USDT SHORT activated (score 0.52) — no tier."""
        signal = xag_short_activated()
        result = format_signal_message(
            signal,
            pending_expiry_minutes=240,
        )
        assert result == snapshot_scrubbed

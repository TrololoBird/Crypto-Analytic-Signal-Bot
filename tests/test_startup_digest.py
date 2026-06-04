"""Startup digest formatting tests."""

from __future__ import annotations

from bot.runtime.startup_digest import format_startup_tracking_digest


def test_startup_digest_lists_repair_and_counts() -> None:
    text = format_startup_tracking_digest(
        {
            "pending": 3,
            "active": 24,
            "repaired": 8,
            "review_closed": 2,
            "stale_expired": 1,
        }
    )
    assert "Pending: <code>3</code>" in text
    assert "Repaired" in text
    assert "Review sweep" in text
    assert "Stale open" in text

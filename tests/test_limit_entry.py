"""Tests for limit-order entry semantics."""

from __future__ import annotations

from bot.domain.limit_entry import (
    limit_delivery_ready,
    should_activate_limit_entry,
    should_activate_limit_fill_price,
)


def test_limit_delivery_rejects_chase_long() -> None:
    ready, reason, _ = limit_delivery_ready(
        direction="long",
        mark_price=101.0,
        entry_low=99.0,
        entry_high=100.0,
        stop=95.0,
        chase_pct=0.005,
    )
    assert ready is False
    assert reason == "limit_late_entry_chase"


def test_limit_delivery_rejects_stop_invalidation_long() -> None:
    ready, reason, _ = limit_delivery_ready(
        direction="long",
        mark_price=94.0,
        entry_low=99.0,
        entry_high=100.0,
        stop=95.0,
    )
    assert ready is False
    assert reason == "limit_publish_rejected"


def test_activate_on_zone_touch_bar() -> None:
    ok, note = should_activate_limit_entry(
        direction="long",
        confirmation_profile="trend_follow",
        entry_low=99.0,
        entry_high=100.0,
        open_=99.6,
        close=99.4,
        high=100.0,
        low=99.2,
    )
    assert ok is True
    assert note == "limit_filled"


def test_activate_on_zone_touch_price() -> None:
    ok, note = should_activate_limit_fill_price(
        entry_low=99.0,
        entry_high=100.0,
        price=99.5,
    )
    assert ok is True
    assert note == "limit_filled"

    ok2, note2 = should_activate_limit_fill_price(
        entry_low=99.0,
        entry_high=100.0,
        price=98.5,
    )
    assert ok2 is False
    assert note2 == "zone_not_touched"

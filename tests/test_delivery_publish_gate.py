"""Offline tests for publish-time limit gate and label normalization."""

from __future__ import annotations

from bot.delivery.trade_plan import evaluate_publish_readiness
from bot.domain.labels import normalize_reject_reason, reject_reason_ru


def test_publish_rejected_when_stop_already_hit_long() -> None:
    ready, reason, _details = evaluate_publish_readiness(
        direction="long",
        mark_price=97.0,
        entry_low=100.0,
        entry_high=101.0,
        stop=98.0,
        chase_pct=0.008,
    )
    assert ready is False
    assert reason == "limit_publish_rejected"


def test_publish_allowed_when_mark_in_zone() -> None:
    ready, reason, _details = evaluate_publish_readiness(
        direction="long",
        mark_price=100.5,
        entry_low=100.0,
        entry_high=101.0,
        stop=98.0,
        chase_pct=0.008,
    )
    assert ready is True
    assert reason is None


def test_legacy_reject_alias_maps_to_publish_rejected() -> None:
    assert normalize_reject_reason("limit_setup_invalidated") == "limit_publish_rejected"
    assert "публикации" in reject_reason_ru("limit_setup_invalidated")

"""Canonical RU label helpers."""

from __future__ import annotations

from bot.domain.labels import (
    REJECT_REASON_RU,
    confluence_leg_label_ru,
    labels_payload,
    normalize_reject_reason,
    reject_reason_ru,
    result_label_ru,
    tracking_event_ru,
)


def test_reject_reason_ru_publish_key() -> None:
    assert reject_reason_ru("limit_publish_rejected") == REJECT_REASON_RU["limit_publish_rejected"]


def test_legacy_alias_normalization() -> None:
    assert normalize_reject_reason("LIMIT_SETUP_INVALIDATED") == "limit_publish_rejected"


def test_internal_tracking_not_channel_cancel() -> None:
    assert "ОТМЕНА" not in tracking_event_ru("setup_invalidated")
    assert "legacy" in tracking_event_ru("setup_invalidated").lower()


def test_labels_payload_has_reject_map() -> None:
    payload = labels_payload()
    assert "limit_publish_rejected" in payload["reject_reasons"]
    assert "stale_15m" in payload["reject_reasons"]
    assert "confluence_legs" in payload
    assert "confirmation_profiles" in payload
    assert result_label_ru("tp1_hit") == "TP1 ✓"


def test_wave_e4_reject_labels() -> None:
    assert reject_reason_ru("stale_1h") == REJECT_REASON_RU["stale_1h"]
    assert reject_reason_ru("atr_too_low") == REJECT_REASON_RU["atr_too_low"]
    assert reject_reason_ru("spread_too_wide") == REJECT_REASON_RU["spread_too_wide"]
    assert reject_reason_ru("risk_reward_too_low") == REJECT_REASON_RU["risk_reward_too_low"]
    assert reject_reason_ru("stop_too_tight") == REJECT_REASON_RU["stop_too_tight"]
    assert (
        reject_reason_ru("adx_penalty_score_too_low")
        == REJECT_REASON_RU["adx_penalty_score_too_low"]
    )
    assert (
        reject_reason_ru("benchmark_context_conflict")
        == REJECT_REASON_RU["benchmark_context_conflict"]
    )
    assert reject_reason_ru("macro_risk_off_long") == REJECT_REASON_RU["macro_risk_off_long"]
    assert reject_reason_ru("5m_opposes_short") == REJECT_REASON_RU["5m_opposes_short"]
    assert normalize_reject_reason("htf_reversal_conflict:1h,4h") == "htf_reversal_conflict"
    assert confluence_leg_label_ru("htf") == "HTF"

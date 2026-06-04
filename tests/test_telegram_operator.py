"""Tests for Telegram operator console helpers."""

from __future__ import annotations

from types import SimpleNamespace

from bot.dashboard.mobile_summary import format_operator_help_text, format_operator_sl_text
from bot.dashboard.operator_actions import (
    format_action_result_html,
    is_tracking_ref,
    normalize_operator_symbol,
)
from bot.runtime.telegram_operator import operator_console_enabled
from bot.secrets import parse_operator_user_ids


def test_parse_operator_user_ids() -> None:
    assert parse_operator_user_ids("") == ()
    assert parse_operator_user_ids("123, 456") == (123, 456)
    assert parse_operator_user_ids("999;888,999") == (888, 999)


def test_operator_console_enabled_requires_token_and_ids() -> None:
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            tg_token="",
            operator_user_ids=(123,),
            notifiers=SimpleNamespace(telegram_operator=SimpleNamespace(enabled=True)),
        )
    )
    assert operator_console_enabled(bot) is False

    bot.settings.tg_token = "123456789:ABCDEFghijklmnop"
    assert operator_console_enabled(bot) is True


def test_format_operator_sl_text_includes_causes() -> None:
    text = format_operator_sl_text(
        {
            "outcomes_7d": {"stop_losses": 2, "win_rate": 0.25},
            "sl_root_causes": {"bear_long_immediate_stop": 1},
            "sl_root_cause_labels": {"bear_long_immediate_stop": "Long в bear без MFE"},
            "recent_stop_losses": [
                {
                    "symbol": "BTCUSDT",
                    "direction": "long",
                    "setup_type": "ema_bounce",
                    "sl_root_cause_label": "Long в bear без MFE",
                }
            ],
        }
    )
    assert "Stop-loss analytics" in text
    assert "bear_long" in text or "Long в bear" in text
    assert "BTCUSDT" in text


def test_format_operator_help_lists_commands() -> None:
    help_text = format_operator_help_text()
    assert "/status" in help_text
    assert "/market" in help_text
    assert "/open" in help_text or "/tracking" in help_text
    assert "/delivery" in help_text
    assert "/symbol" in help_text
    assert "/scan" in help_text
    assert "/analyze" in help_text
    assert "Канал" in help_text or "канал" in help_text
    assert "TARGET_CHAT_ID" in help_text


def test_normalize_operator_symbol() -> None:
    assert normalize_operator_symbol("btc") == "BTCUSDT"
    assert normalize_operator_symbol("BTCUSDT") == "BTCUSDT"
    assert normalize_operator_symbol("6C8101D5") is None
    assert is_tracking_ref("6C8101D5") is True
    assert is_tracking_ref("BTCUSDT") is False


def test_format_action_result_html() -> None:
    text = format_action_result_html("Scan", {"ok": True, "delivered": 2, "elapsed_s": 12.3})
    assert "Scan" in text
    assert "delivered" in text
    err = format_action_result_html("Refresh", {"error": "timeout"})
    assert "timeout" in err

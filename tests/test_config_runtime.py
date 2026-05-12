from __future__ import annotations

from pathlib import Path

import pytest

from bot.domain.config import BotSettings, load_settings


def test_runtime_strategy_executor_settings_are_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[bot]
[bot.runtime]
strategy_concurrency = 7
strategy_timeout_seconds = 9.5
futures_data_request_limit_per_5m = 240
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_TOKEN", "")
    monkeypatch.setenv("TARGET_CHAT_ID", "")

    settings = load_settings(config_file)

    assert settings.runtime.strategy_concurrency == 7
    assert settings.runtime.strategy_timeout_seconds == pytest.approx(9.5)
    assert settings.runtime.futures_data_request_limit_per_5m == 240


def test_config_example_supports_disabled_notifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_TOKEN", "")
    monkeypatch.setenv("TARGET_CHAT_ID", "")

    settings = load_settings(Path("config.toml.example"))

    assert settings.notifiers.provider == "none"


def test_phase_53_strategy_flags_are_runtime_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_TOKEN", "")
    monkeypatch.setenv("TARGET_CHAT_ID", "")

    settings = load_settings(Path("config.toml"))
    enabled = set(settings.setups.enabled_setup_ids())

    roadmap_ids = {
        "vwap_trend",
        "supertrend_follow",
        "price_velocity",
        "volume_anomaly",
        "volume_climax_reversal",
        "keltner_breakout",
        "whale_walls",
        "spread_strategy",
        "depth_imbalance",
        "absorption",
        "aggression_shift",
        "liquidation_heatmap",
        "stop_hunt_detection",
        "multi_tf_trend",
        "rsi_divergence_bottom",
        "wyckoff_spring",
        "bb_squeeze",
        "atr_expansion",
        "ls_ratio_extreme",
        "oi_divergence",
        "btc_correlation",
        "altcoin_season_index",
    }

    assert roadmap_ids.issubset(enabled)
    for setup_id in roadmap_ids:
        assert setup_id in settings.filters.setups


def test_shortlist_spread_is_not_looser_than_filter_spread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_TOKEN", "")
    monkeypatch.setenv("TARGET_CHAT_ID", "")

    for path in (Path("config.toml"), Path("config.toml.example")):
        settings = load_settings(path)
        assert (
            settings.universe.shortlist_spread_max_bps
            <= settings.filters.max_spread_bps
        )


def test_ws_subscribe_delay_respects_binance_control_message_limit() -> None:
    with pytest.raises(ValueError):
        BotSettings(
            tg_token="1" * 30,
            target_chat_id="123",
            ws={"subscribe_chunk_delay_ms": 50},
        )


def test_load_settings_prefers_legacy_double_dot_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config..toml").write_text(
        """
[bot]
[bot.runtime]
strategy_concurrency = 9
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "config.toml.example").write_text(
        """
[bot]
[bot.runtime]
strategy_concurrency = 3
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_TOKEN", "")
    monkeypatch.setenv("TARGET_CHAT_ID", "")

    settings = load_settings(tmp_path / "config.toml")

    assert settings.runtime.strategy_concurrency == 9
    assert settings.config_path.name == "config..toml"


def test_load_settings_enables_telegram_when_example_fallback_has_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.toml.example").write_text(
        """
[bot]
[bot.notifiers]
provider = "none"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_TOKEN", "123456789:abcdefghijklmnopqrstuvwxyzABCDE")
    monkeypatch.setenv("TARGET_CHAT_ID", "123456")

    settings = load_settings(tmp_path / "config.toml")

    assert settings.config_path.name == "config.toml.example"
    assert settings.notifiers.provider == "telegram"


def test_load_settings_enables_telegram_when_explicit_none_has_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[bot]
[bot.notifiers]
provider = "none"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TG_TOKEN", "123456789:abcdefghijklmnopqrstuvwxyzABCDE")
    monkeypatch.setenv("TARGET_CHAT_ID", "123456")

    settings = load_settings(config_file)

    assert settings.notifiers.provider == "telegram"

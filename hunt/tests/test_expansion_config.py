from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hunt_core.deliver.telegram import DisabledBroadcaster, build_message_broadcaster
from hunt_core.domain import config as domain_config
from hunt_core.expansion import config as expansion_config
from hunt_core.secrets import load_secrets


def test_load_expansion_config_reads_grouped_sections(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    defaults = root / "config.defaults.toml"
    defaults.write_text(
        """
[hunt.expansion.runtime]
enabled = false
mode = "lab"
review_loop = true

[hunt.expansion.persistence]
history_persist = false
runtime_save_interval_s = 120.0

[hunt.expansion.thresholds]
forecast_min_quality = 0.66
execution_min_trigger = 0.61

[hunt.expansion.telegram]
tg_min_quality = 0.51
tg_universe_scan = true
""",
        encoding="utf-8",
    )
    (root / "config.toml").write_text("", encoding="utf-8")

    monkeypatch.setattr(expansion_config, "ROOT", root)
    monkeypatch.setattr(expansion_config, "_defaults_path", lambda: defaults)
    monkeypatch.setattr(expansion_config, "_repo_config_path", lambda: root / "config.toml")
    monkeypatch.setattr(expansion_config, "EXPANSION_CALIBRATION_JSON", root / "expansion_calibration.json")

    config = expansion_config.load_expansion_config.cache_clear() or expansion_config.load_expansion_config()

    assert config.enabled is False
    assert config.mode == "lab"
    assert config.review_loop is True
    assert config.history_persist is False
    assert config.runtime_save_interval_s == 120.0
    assert config.forecast_min_quality == 0.66
    assert config.execution_min_trigger == 0.61
    assert config.tg_min_quality == 0.51
    assert config.tg_universe_scan is True


def test_load_settings_prefers_user_config_as_single_source(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    defaults = root / "config.defaults.toml"
    defaults.write_text(
        """
[bot]
log_level = "WARNING"

[bot.network]
proxy_url = "https://default.example"
""",
        encoding="utf-8",
    )
    user_config = root / "config.toml"
    user_config.write_text(
        """
[bot]
log_level = "DEBUG"

[bot.network]
proxy_url = "https://user.example"
""",
        encoding="utf-8",
    )

    monkeypatch.chdir(root)

    settings = domain_config.load_settings(user_config)

    assert settings.runtime.log_level == "DEBUG"
    assert settings.network.proxy_url == "https://user.example"


def test_build_message_broadcaster_disables_when_telegram_unconfigured():
    settings = SimpleNamespace(
        tg_token="",
        target_chat_id="",
        notifiers=SimpleNamespace(provider="telegram"),
    )

    broadcaster = build_message_broadcaster(settings)

    assert isinstance(broadcaster, DisabledBroadcaster)


def test_load_secrets_reads_dotenv_from_repo_ancestor(tmp_path, monkeypatch):
    repo_root = tmp_path / "workspace" / "repo"
    repo_root.mkdir(parents=True)
    env_dir = tmp_path / "workspace"
    (env_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=ancestor-token\nTELEGRAM_CHAT_ID=456\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")

    secrets = load_secrets(repo_root)

    assert secrets.tg_token == "ancestor-token"
    assert secrets.target_chat_id == "456"


def test_analysis_expansion_engine_module_reexports_loader():
    from hunt_core.expansion import config as shim_config
    from hunt_core.expansion.config import load_expansion_config

    assert shim_config.load_expansion_config is load_expansion_config

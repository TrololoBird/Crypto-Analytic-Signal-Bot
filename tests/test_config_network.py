"""Config: network proxy section."""

from __future__ import annotations

import pytest

from bot.domain.config import NetworkConfig, load_settings


def test_network_config_defaults() -> None:
    cfg = NetworkConfig()
    assert cfg.proxy_url is None
    assert cfg.trust_env is True


def test_network_proxy_url_normalized_empty() -> None:
    cfg = NetworkConfig(proxy_url="   ")
    assert cfg.proxy_url is None


def test_load_settings_merges_binance_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_PROXY_URL", "socks5h://127.0.0.1:7890")
    settings = load_settings("config.toml.example")
    assert settings.network.proxy_url == "socks5h://127.0.0.1:7890"

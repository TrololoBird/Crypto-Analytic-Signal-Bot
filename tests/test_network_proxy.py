"""Unit tests for Binance egress proxy resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bot.market.network_proxy import is_socks_proxy, mask_proxy_url, resolve_proxy_url

if TYPE_CHECKING:
    import pytest


def test_resolve_proxy_prefers_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://env:8080")
    assert resolve_proxy_url(config_url="socks5h://127.0.0.1:7890") == "socks5h://127.0.0.1:7890"


def test_resolve_proxy_reads_env_when_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_PROXY_URL", "socks5h://127.0.0.1:1080")
    assert resolve_proxy_url(trust_env=True) == "socks5h://127.0.0.1:1080"


def test_resolve_proxy_ignores_env_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://env:8080")
    assert resolve_proxy_url(trust_env=False) is None


def test_mask_proxy_url_hides_credentials() -> None:
    masked = mask_proxy_url("socks5://user:secret@proxy.example:1080")
    assert "secret" not in masked
    assert "proxy.example" in masked


def test_is_socks_proxy() -> None:
    assert is_socks_proxy("socks5h://127.0.0.1:7890")
    assert not is_socks_proxy("http://127.0.0.1:7890")

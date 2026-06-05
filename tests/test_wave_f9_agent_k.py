"""Wave F9 / Agent K: domain config catalog alignment, strict TOML, labels, runtime contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from bot.domain.config import BotSettings, RuntimeConfig, SetupConfig, load_settings
from bot.domain.contracts import assert_runtime_call_path_is_clean
from bot.domain.labels import reject_reason_ru
from bot.domain.strategy_catalog import (
    CATALOG_SETUP_IDS,
    CATALOG_SETUP_IDS_ORDERED,
    verify_config_setup_references,
    verify_setup_config_model,
)

if TYPE_CHECKING:
    from pathlib import Path


def _minimal_settings(**overrides: object) -> BotSettings:
    payload: dict[str, object] = {
        "tg_token": "123456789012345678901234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "target_chat_id": "-1001234567890",
    }
    payload.update(overrides)
    return BotSettings.model_validate(payload)


def test_k1_setup_ids_derived_from_catalog() -> None:
    assert len(CATALOG_SETUP_IDS) == 38
    assert len(CATALOG_SETUP_IDS_ORDERED) == 38
    assert verify_setup_config_model(SetupConfig) == []
    settings = _minimal_settings()
    assert verify_config_setup_references(settings) == []
    enabled = settings.setups.enabled_setup_ids()
    assert len(enabled) == 38
    assert set(enabled) == CATALOG_SETUP_IDS


def test_k1_rejects_unknown_filters_setup_override() -> None:
    settings = _minimal_settings(
        filters={"setups": {"not_a_real_setup": {"min_rr": 2.0}}},
    )
    errors = verify_config_setup_references(settings)
    assert len(errors) == 1
    assert "not_a_real_setup" in errors[0]


def test_k2_strict_toml_rejects_unknown_runtime_key() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RuntimeConfig.model_validate({"orphan_runtime_key_xyz": True})


def test_k2_strict_toml_rejects_unknown_bot_key() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _minimal_settings(unknown_section={"foo": 1})


def test_k2_load_settings_rejects_orphan_toml_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[bot.runtime]
analysis_concurrency = 6
orphan_runtime_key = true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="orphan_runtime_key"):
        load_settings(config)


@pytest.mark.parametrize(
    ("code", "expected_substring"),
    [
        ("catalog_min_volume", "объём"),
        ("catalog_min_adx_1h", "ADX"),
        ("catalog_htf_bias_conflict", "HTF"),
        ("spread_unavailable", "спред"),
        ("atr_unavailable", "ATR"),
        ("htf_frames_missing", "HTF"),
        ("regime_not_suitable", "режим"),
        ("data.funding_rate_missing", "funding"),
        ("data.orderbook_not_ready", "orderbook"),
        ("data.custom_missing_field", "данные:"),
    ],
)
def test_k4_reject_reason_ru_expanded(code: str, expected_substring: str) -> None:
    label = reject_reason_ru(code)
    assert expected_substring.lower() in label.lower()


def test_k7_runtime_call_path_is_clean() -> None:
    assert assert_runtime_call_path_is_clean() is None


def test_k7_validate_for_runtime_invokes_catalog_and_call_path(tmp_path: Path) -> None:
    settings = _minimal_settings(data_dir=tmp_path / "data" / "bot")
    settings.validate_for_runtime(require_telegram=False)

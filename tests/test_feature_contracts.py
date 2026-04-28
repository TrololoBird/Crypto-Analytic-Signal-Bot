from __future__ import annotations

from dataclasses import fields
import pytest

from bot.feature_contract import (
    PUBLIC_FEATURE_FIELDS,
    PUBLIC_FEATURE_SCHEMA_VERSION,
    normalize_public_feature_payload,
    validate_public_feature_payload,
)
from bot.models import PreparedSymbol
from bot.outcomes import build_prepared_feature_snapshot
from bot.runtime_contract import (
    RUNTIME_PUBLIC_IMPORT_CONTRACT,
    assert_runtime_call_path_is_clean,
    assert_runtime_import_contract,
)


def test_public_feature_contract_matches_prepared_symbol_snapshot_surface() -> None:
    payload_fields = set(PUBLIC_FEATURE_FIELDS)
    prepared_fields = {f.name for f in fields(PreparedSymbol)}

    missing_on_prepared = {
        "data_source_mix",
        "market_regime",
        "rsi_15m",
        "rsi_1h",
        "rsi_4h",
        "adx_1h",
        "adx_4h",
        "atr_pct_15m",
        "volume_ratio_15m",
        "macd_histogram_15m",
        "ema20_above_ema50_15m",
        "ema50_above_ema200_15m",
        "ema20_above_ema50_1h",
        "ema50_above_ema200_1h",
        "supertrend_dir_1h",
        "supertrend_dir_15m",
        "obv_above_ema_15m",
        "bb_pct_b_15m",
        "bb_width_15m",
    }

    # Most fields are direct PreparedSymbol attrs; derived frame fields are explicit exceptions.
    assert (payload_fields - missing_on_prepared).issubset(prepared_fields)


def test_validate_public_feature_payload_rejects_missing_field() -> None:
    payload = {name: None for name in PUBLIC_FEATURE_FIELDS}
    payload.pop("rsi_15m")

    with pytest.raises(ValueError, match="missing"):
        validate_public_feature_payload(payload)


def test_validate_public_feature_payload_rejects_extra_field() -> None:
    payload = {name: None for name in PUBLIC_FEATURE_FIELDS}
    payload["experimental_scaffold_field"] = 1

    with pytest.raises(ValueError, match="extra"):
        validate_public_feature_payload(payload)


def test_normalize_public_feature_payload_rejects_incompatible_schema_growth() -> None:
    incompatible = {name: None for name in PUBLIC_FEATURE_FIELDS}
    incompatible["v2_only_new_feature"] = 123

    with pytest.raises(ValueError, match="schema mismatch"):
        normalize_public_feature_payload(incompatible)


def test_build_prepared_feature_snapshot_enforces_exact_schema() -> None:
    payload = build_prepared_feature_snapshot(None)

    assert tuple(payload.keys()) == PUBLIC_FEATURE_FIELDS


def test_runtime_call_path_has_no_scaffold_imports() -> None:
    assert_runtime_call_path_is_clean()


def test_runtime_import_contract_rejects_scaffold_fragment() -> None:
    with pytest.raises(ValueError, match="blocked import fragment"):
        assert_runtime_import_contract({"bot.experimental.pipeline"})


def test_runtime_public_import_contract_is_explicit_and_stable() -> None:
    import bot

    assert tuple(bot.__all__) == RUNTIME_PUBLIC_IMPORT_CONTRACT


def test_feature_contract_version_is_pinned_for_compatibility() -> None:
    assert PUBLIC_FEATURE_SCHEMA_VERSION == "v1"

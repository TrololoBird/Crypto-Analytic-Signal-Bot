from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from bot.feature_contract import (
    PUBLIC_FEATURE_FIELDS,
    validate_public_feature_payload,
)
from bot.models import PreparedSymbol
from bot.outcomes import build_prepared_feature_snapshot


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


def test_build_prepared_feature_snapshot_enforces_exact_schema() -> None:
    payload = build_prepared_feature_snapshot(None)

    assert tuple(payload.keys()) == PUBLIC_FEATURE_FIELDS


def test_runtime_call_path_has_no_scaffold_imports() -> None:
    import ast

    runtime_files = [
        Path("bot/application/bot.py"),
        Path("bot/application/symbol_analyzer.py"),
        Path("bot/core/engine/engine.py"),
        Path("bot/strategies/__init__.py"),
    ]

    imported_names: set[str] = set()
    for file_path in runtime_files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)

    assert all("scaffold" not in name and "experimental" not in name for name in imported_names)

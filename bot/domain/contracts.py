from __future__ import annotations

import ast
from pathlib import Path
from collections.abc import Mapping
from typing import Any

# --- Feature Contract ---

# Public runtime feature payload contract persisted from prepared symbol snapshots.
PUBLIC_FEATURE_SCHEMA_VERSION = "v1"
PUBLIC_FEATURE_FIELDS: tuple[str, ...] = (
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
    "funding_rate",
    "oi_current",
    "oi_change_pct",
    "oi_slope_5m",
    "ls_ratio",
    "global_ls_ratio",
    "top_trader_position_ratio",
    "top_vs_global_ls_gap",
    "liquidation_score",
    "mark_index_spread_bps",
    "premium_zscore_5m",
    "premium_slope_5m",
    "context_snapshot_age_seconds",
    "depth_imbalance",
    "microprice_bias",
    "agg_trade_delta_30s",
    "aggression_shift",
    "spot_lead_return_1m",
    "spot_futures_spread_bps",
    "mark_price_age_seconds",
    "ticker_price_age_seconds",
    "book_ticker_age_seconds",
    "data_source_mix",
    "market_regime",
)


def validate_public_feature_payload(payload: Mapping[str, Any]) -> None:
    expected = set(PUBLIC_FEATURE_FIELDS)
    provided = set(payload.keys())

    missing = tuple(sorted(expected - provided))
    extra = tuple(sorted(provided - expected))
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"extra={extra}")
        raise ValueError(
            "public feature payload schema mismatch: " + "; ".join(details)
        )


def normalize_public_feature_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    validate_public_feature_payload(payload)
    return {name: payload.get(name) for name in PUBLIC_FEATURE_FIELDS}


# --- Runtime Contract ---

RUNTIME_CALL_PATH_FILES: tuple[Path, ...] = (
    Path("main.py"),
    Path("bot/cli.py"),
    Path("bot/__init__.py"),
    Path("bot/application/bot.py"),
)

RUNTIME_PUBLIC_IMPORT_CONTRACT: tuple[str, ...] = (
    "SignalBot",
    "BotSettings",
    "load_settings",
)

SCAFFOLD_IMPORT_BLOCKLIST: tuple[str, ...] = (
    "bot.telegram.bot",
    "scaffold",
    "experimental",
    "prototype",
)


def imported_module_names(file_path: Path) -> set[str]:
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    return imported_names


def assert_runtime_import_contract(imported_names: set[str]) -> None:
    for blocked in SCAFFOLD_IMPORT_BLOCKLIST:
        if any(blocked in name for name in imported_names):
            raise ValueError(
                f"runtime import contract violation: blocked import fragment {blocked!r}"
            )


def assert_runtime_call_path_is_clean() -> None:
    imported_names: set[str] = set()
    for file_path in RUNTIME_CALL_PATH_FILES:
        imported_names.update(imported_module_names(file_path))
    assert_runtime_import_contract(imported_names)

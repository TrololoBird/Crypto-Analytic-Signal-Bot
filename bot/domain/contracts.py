from __future__ import annotations

import ast
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.runtime.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from collections.abc import Mapping

LOG = logging.getLogger("bot.contracts")

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
    "vah_1h",
    "val_1h",
    "vah_15m",
    "val_15m",
    "funding_rate_zscore_48h",
    "liquidation_cascade_5m",
)
PRIVATE_KEYS = {"balance", "position", "order", "account", "margin"}


def validate_public_feature_payload(payload: Mapping[str, Any]) -> None:
    if any(key in payload for key in PRIVATE_KEYS):
        msg = f"Private data in public feature payload: {payload.keys()}"
        raise ValueError(msg)
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
        raise ValueError("public feature payload schema mismatch: " + "; ".join(details))


def normalize_public_feature_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    validate_public_feature_payload(payload)
    return {name: payload.get(name) for name in PUBLIC_FEATURE_FIELDS}


def _normalized_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        parsed = float(value)
    except TypeError, ValueError:
        return default
    if math.isnan(parsed) or math.isinf(parsed):
        return default
    return parsed


def _normalized_bool(value: Any, *, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return bool(value)


def build_public_feature_snapshot(prepared: Any) -> dict[str, Any]:
    """Build a normalized public feature snapshot from PreparedSymbol-like data."""
    if prepared is None:
        return normalize_public_feature_payload(dict.fromkeys(PUBLIC_FEATURE_FIELDS))

    features: dict[str, Any] = {}

    def _frame_value(frame: Any, column: str) -> float | None:
        if frame is None or getattr(frame, "is_empty", lambda: True)():
            return None
        if column not in getattr(frame, "columns", []):
            return None
        try:
            return _normalized_float(frame.item(-1, column))
        except DEFENSIVE_EXC as exc:
            LOG.debug("public feature snapshot read failed | column=%s error=%s", column, exc)
            return None

    def _ema_stack(frame: Any, fast: str, slow: str) -> bool | None:
        fast_value = _frame_value(frame, fast)
        slow_value = _frame_value(frame, slow)
        if fast_value is None or slow_value is None or slow_value <= 0.0:
            return None
        return fast_value > slow_value

    work_15m = getattr(prepared, "work_15m", None)
    work_1h = getattr(prepared, "work_1h", None)
    work_4h = getattr(prepared, "work_4h", None)

    features["rsi_15m"] = _frame_value(work_15m, "rsi14")
    features["rsi_1h"] = _frame_value(work_1h, "rsi14")
    features["rsi_4h"] = _frame_value(work_4h, "rsi14")
    features["adx_1h"] = _frame_value(work_1h, "adx14")
    features["adx_4h"] = _frame_value(work_4h, "adx14")
    features["atr_pct_15m"] = _frame_value(work_15m, "atr_pct")
    features["volume_ratio_15m"] = _frame_value(work_15m, "volume_ratio20")
    features["macd_histogram_15m"] = _frame_value(work_15m, "macd_hist")

    features["ema20_above_ema50_15m"] = _normalized_bool(_ema_stack(work_15m, "ema20", "ema50"))
    features["ema50_above_ema200_15m"] = _normalized_bool(_ema_stack(work_15m, "ema50", "ema200"))
    features["ema20_above_ema50_1h"] = _normalized_bool(_ema_stack(work_1h, "ema20", "ema50"))
    features["ema50_above_ema200_1h"] = _normalized_bool(_ema_stack(work_1h, "ema50", "ema200"))

    features["supertrend_dir_1h"] = _frame_value(work_1h, "supertrend_dir")
    features["supertrend_dir_15m"] = _frame_value(work_15m, "supertrend_dir")
    features["obv_above_ema_15m"] = _frame_value(work_15m, "obv_above_ema")
    features["bb_pct_b_15m"] = _frame_value(work_15m, "bb_pct_b")
    features["bb_width_15m"] = _frame_value(work_15m, "bb_width")

    features["funding_rate"] = _normalized_float(getattr(prepared, "funding_rate", None))
    features["oi_current"] = _normalized_float(getattr(prepared, "oi_current", None))
    features["oi_change_pct"] = _normalized_float(getattr(prepared, "oi_change_pct", None))
    features["oi_slope_5m"] = _normalized_float(getattr(prepared, "oi_slope_5m", None))
    features["ls_ratio"] = _normalized_float(getattr(prepared, "ls_ratio", None))
    features["global_ls_ratio"] = _normalized_float(getattr(prepared, "global_ls_ratio", None))
    features["top_trader_position_ratio"] = _normalized_float(
        getattr(prepared, "top_trader_position_ratio", None)
    )
    features["top_vs_global_ls_gap"] = _normalized_float(
        getattr(prepared, "top_vs_global_ls_gap", None)
    )
    features["liquidation_score"] = _normalized_float(getattr(prepared, "liquidation_score", None))
    features["mark_index_spread_bps"] = _normalized_float(
        getattr(prepared, "mark_index_spread_bps", None)
    )
    features["premium_zscore_5m"] = _normalized_float(getattr(prepared, "premium_zscore_5m", None))
    features["premium_slope_5m"] = _normalized_float(getattr(prepared, "premium_slope_5m", None))
    features["context_snapshot_age_seconds"] = _normalized_float(
        getattr(prepared, "context_snapshot_age_seconds", None)
    )
    features["depth_imbalance"] = _normalized_float(getattr(prepared, "depth_imbalance", None))
    features["microprice_bias"] = _normalized_float(getattr(prepared, "microprice_bias", None))
    features["agg_trade_delta_30s"] = _normalized_float(
        getattr(prepared, "agg_trade_delta_30s", None)
    )
    features["aggression_shift"] = _normalized_float(getattr(prepared, "aggression_shift", None))
    features["spot_lead_return_1m"] = _normalized_float(
        getattr(prepared, "spot_lead_return_1m", None)
    )
    features["spot_futures_spread_bps"] = _normalized_float(
        getattr(prepared, "spot_futures_spread_bps", None)
    )
    features["mark_price_age_seconds"] = _normalized_float(
        getattr(prepared, "mark_price_age_seconds", None)
    )
    features["ticker_price_age_seconds"] = _normalized_float(
        getattr(prepared, "ticker_price_age_seconds", None)
    )
    features["book_ticker_age_seconds"] = _normalized_float(
        getattr(prepared, "book_ticker_age_seconds", None)
    )
    features["data_source_mix"] = (
        getattr(prepared, "data_source_mix", "futures_only") or "futures_only"
    )
    features["market_regime"] = getattr(prepared, "market_regime", "neutral") or "neutral"
    features["vah_1h"] = _normalized_float(getattr(prepared, "vah_1h", None))
    features["val_1h"] = _normalized_float(getattr(prepared, "val_1h", None))
    features["vah_15m"] = _normalized_float(getattr(prepared, "vah_15m", None))
    features["val_15m"] = _normalized_float(getattr(prepared, "val_15m", None))
    features["funding_rate_zscore_48h"] = _normalized_float(
        getattr(prepared, "funding_rate_zscore_48h", None)
    )
    cascade = getattr(prepared, "liquidation_cascade_5m", None)
    if cascade is None:
        features["liquidation_cascade_5m"] = None
    else:
        features["liquidation_cascade_5m"] = bool(cascade)

    return normalize_public_feature_payload(features)


# --- Runtime Contract ---

RUNTIME_CALL_PATH_FILES: tuple[Path, ...] = (
    Path("main.py"),
    Path("bot/cli.py"),
    Path("bot/__init__.py"),
    Path("bot/runtime/bot.py"),
)

RUNTIME_PUBLIC_IMPORT_CONTRACT: tuple[str, ...] = (
    "SignalBot",
    "BotSettings",
    "load_settings",
)

SCAFFOLD_IMPORT_BLOCKLIST: tuple[str, ...] = (
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
            msg = f"runtime import contract violation: blocked import fragment {blocked!r}"
            raise ValueError(msg)


def assert_runtime_call_path_is_clean() -> None:
    imported_names: set[str] = set()
    for file_path in RUNTIME_CALL_PATH_FILES:
        imported_names.update(imported_module_names(file_path))
    assert_runtime_import_contract(imported_names)

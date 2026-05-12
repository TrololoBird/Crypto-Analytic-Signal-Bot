#!/usr/bin/env python3
"""Локальная валидация окружения/конфига и базовой целостности фичей."""

from __future__ import annotations

import argparse
import sys
import math
from pathlib import Path
from typing import Iterable, Sequence

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


VALID_INTERVALS = {
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
}


def _split_values(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for item in str(value or "").split(","):
            normalized = item.strip()
            if normalized:
                result.append(normalized)
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate bot config and local feature/strategy invariants."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="TOML config path to validate; config.toml falls back to config.toml.example.",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Optional symbol filter for operator checks. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--interval",
        action="append",
        default=[],
        help="Optional interval list to validate. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=120,
        help="Expected minimum bar request size for live checks.",
    )
    return parser.parse_args(argv)


def _validate_symbols(symbols: list[str], errors: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = raw.strip().upper()
        if not symbol:
            continue
        if not symbol.endswith("USDT") or not symbol.replace("_", "").isalnum():
            errors.append(f"Invalid symbol format: {raw}")
            continue
        if symbol not in seen:
            seen.add(symbol)
            normalized.append(symbol)
    return normalized


def _validate_intervals(intervals: list[str], errors: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in intervals:
        interval = raw.strip()
        if interval not in VALID_INTERVALS:
            errors.append(f"Invalid interval: {raw}")
            continue
        if interval not in seen:
            seen.add(interval)
            normalized.append(interval)
    return normalized


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    errors: list[str] = []

    try:
        from bot.domain.config import load_settings

        config_path = Path(args.config)
        if not config_path.exists() and config_path.name != "config.toml":
            errors.append(f"Config file not found: {config_path}")
        settings = load_settings(config_path)
        settings.validate_for_runtime(require_telegram=settings.notifiers.provider == "telegram")
    except Exception as exc:  # pragma: no cover - defensive CLI script
        errors.append(f"Config validation failed: {exc}")

    symbols = _validate_symbols(_split_values(args.symbol), errors)
    intervals = _validate_intervals(_split_values(args.interval), errors)
    if args.limit < 30:
        errors.append("--limit must be >= 30")

    # 1) Базовые папки
    for directory in ("data", "logs"):
        path = REPO_ROOT / directory
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)

    # 2) Импорт экспортов стратегий
    try:
        from bot.strategies import STRATEGY_CLASSES

        if not STRATEGY_CLASSES:
            errors.append("No strategies exported via bot.strategies.STRATEGY_CLASSES")
    except Exception as exc:  # pragma: no cover - defensive CLI script
        errors.append(f"Strategy import failed: {exc}")

    # 3) Проверка advanced indicators на константные placeholder-профили
    try:
        from bot.features import _add_advanced_indicators

        closes = [100.0 + math.sin(i / 4.0) * 3.0 + i * 0.02 for i in range(120)]
        df = pl.DataFrame(
            {
                "open": [close - 0.2 for close in closes],
                "high": [close + 1.0 + (idx % 5) * 0.05 for idx, close in enumerate(closes)],
                "low": [close - 1.0 - (idx % 7) * 0.05 for idx, close in enumerate(closes)],
                "close": closes,
                "volume": [1000.0 + ((idx % 17) * 20.0) for idx in range(120)],
            }
        )
        out = _add_advanced_indicators(df)
        must_vary = (
            "aroon_up14",
            "aroon_down14",
            "fisher",
            "fisher_signal",
            "squeeze_hist",
            "chandelier_dir",
        )
        for column in must_vary:
            if column not in out.columns:
                errors.append(f"Missing indicator column: {column}")
                continue
            values = out[column].drop_nulls()
            if values.len() == 0:
                errors.append(f"Indicator empty after drop_nulls: {column}")
                continue
            uniq = values.unique().len()
            if uniq <= 1:
                only_value = float(values[0])
                if only_value in {0.0, 50.0}:
                    errors.append(
                        f"Indicator appears placeholder-like constant ({only_value}): {column}"
                    )
    except Exception as exc:  # pragma: no cover - defensive CLI script
        errors.append(f"Indicator validation failed: {exc}")

    if errors:
        print("VALIDATION FAILED:")
        for error in errors:
            print(f"  [FAIL] {error}")
        return 1

    print(
        "[OK] All checks passed | "
        f"config={Path(args.config)} "
        f"symbols={symbols or 'default'} "
        f"intervals={intervals or 'config'} "
        f"limit={args.limit}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

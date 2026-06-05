#!/usr/bin/env python3
"""v9 refactor gate — forbidden legacy paths and import smoke."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bot.domain.strategy_catalog import PR10_WAVES, verify_strategy_wiring, wave_status
from bot.runtime.bot import SignalBot  # noqa: F401
from bot.runtime.errors import DEFENSIVE_EXC
from bot.strategies import STRATEGY_CLASSES

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FORBIDDEN_PATHS = (
    "bot/setups/detectors",
    "bot/application",
    "bot/telegram",
    "bot/websocket",
    "bot/infrastructure",
    "bot/core/engine",
    "bot/core/memory",
    "bot/core/diagnostics",
    "bot/core/analyzer",
    "bot/market/ws_lib",
    "bot/delivery.py",
    "bot/features.py",
    "bot/market_data.py",
    "bot/ws_manager.py",
    "bot/messaging.py",
    "bot/confluence.py",
    "bot/signal_contract.py",
    "bot/startup_reporter.py",
    "bot/signal_diagnostics.py",
)

FORBIDDEN_IMPORT_SNIPPETS = (
    "from bot.setups.detectors",
    "from bot.application",
    "import bot.application",
    "from bot.market_data",
    "from bot.ws_manager",
    "from bot.infrastructure.binance_client",
    "from bot.telegram",
)

# Signal-only: no private Binance / CCXT on the hot path (see CONNECTOR_DECISION.md).
BOT_PRIVATE_API_IMPORT_SNIPPETS = (
    "import ccxt",
    "from ccxt",
    "import ccxt.pro",
    "from ccxt.pro",
    "from binance.client",
    "from binance.um_futures",
    "from binance_futures_connector",
    "import binance",
    "from python_binance",
    "import python_binance",
)


def _scan_imports() -> list[str]:
    errors: list[str] = []
    skip = {Path("scripts/verify_refactor_gate.py")}
    for root in (REPO_ROOT / "bot", REPO_ROOT / "scripts", REPO_ROOT / "tests"):
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            rel = py.relative_to(REPO_ROOT)
            if rel.as_posix() in {p.as_posix() for p in skip}:
                continue
            try:
                lines = py.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                stripped = line.strip()
                if not stripped.startswith(("from ", "import ")):
                    continue
                for snippet in FORBIDDEN_IMPORT_SNIPPETS:
                    if snippet in stripped:
                        errors.append(f"{rel}: forbidden import {snippet!r}")
                        break
    return errors


def _scan_bot_private_api_imports() -> list[str]:
    errors: list[str] = []
    root = REPO_ROOT / "bot"
    if not root.exists():
        return errors
    for py in root.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        rel = py.relative_to(REPO_ROOT)
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith(("from ", "import ")):
                continue
            for snippet in BOT_PRIVATE_API_IMPORT_SNIPPETS:
                if snippet in stripped:
                    errors.append(f"{rel}: private/ccxt import {snippet!r}")
                    break
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="v9 refactor structural gate")
    parser.parse_args()
    errors: list[str] = [
        f"legacy path still exists: {rel}" for rel in FORBIDDEN_PATHS if (REPO_ROOT / rel).exists()
    ]

    errors.extend(_scan_imports())
    errors.extend(_scan_bot_private_api_imports())

    try:
        errors.extend(verify_strategy_wiring(STRATEGY_CLASSES))
        waves = wave_status(STRATEGY_CLASSES)
        for wave, ok in sorted(waves.items()):
            if not ok:
                missing = sorted(PR10_WAVES[wave] - {c.setup_id for c in STRATEGY_CLASSES})
                errors.append(f"PR10 wave {wave} incomplete: missing {missing}")
    except DEFENSIVE_EXC as exc:
        errors.append(f"import/wiring check failed: {exc}")

    if errors:
        print("REFACTOR GATE FAILED:")
        for err in errors:
            print(f"  [FAIL] {err}")
        return 1

    print(
        "[OK] v9 refactor gate passed (paths, imports, 38-strategy catalog wiring, PR10 waves 1-5)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

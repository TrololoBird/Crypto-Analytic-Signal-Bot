from __future__ import annotations

import ast
from pathlib import Path

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
    "bot.telegram_bot",
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

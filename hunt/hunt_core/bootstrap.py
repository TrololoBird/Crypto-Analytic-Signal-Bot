"""Add monorepo root + hunt/ to sys.path; verify Polars feature stack."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

_FEATURE_STACK: tuple[str, ...] = (
    "polars",
    "polars_ta",
    "polars_ols",
    "polars_ds",
    "polars_trading",
)


def bootstrap() -> Path:
    hunt_root = Path(__file__).resolve().parents[1]
    repo = hunt_root.parent
    for p in (str(repo), str(hunt_root)):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("POLARS_STREAMING", "1")
    return repo


def require_feature_stack() -> None:
    """Fail fast when core Polars TA dependencies are missing."""
    missing: list[str] = []
    for mod in _FEATURE_STACK:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        raise ImportError(
            "Hunt requires Polars feature stack: "
            f"{', '.join(missing)}. Install: pip install -e hunt/"
        )


__all__ = ["bootstrap", "require_feature_stack"]

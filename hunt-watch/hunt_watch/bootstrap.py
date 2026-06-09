"""Add monorepo root + hunt-watch/ to sys.path for script entrypoints."""

from __future__ import annotations

import sys
from pathlib import Path


def bootstrap() -> Path:
    hunt_root = Path(__file__).resolve().parents[1]
    repo = hunt_root.parent
    for p in (str(repo), str(hunt_root)):
        if p not in sys.path:
            sys.path.insert(0, p)
    return repo

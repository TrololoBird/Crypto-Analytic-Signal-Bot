"""Factor lifecycle registry — production vs quarantine (no _dev import on hot path)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REGISTRY = Path(__file__).with_name("factor_registry.json")


def load_factor_registry() -> dict[str, Any]:
    if not _REGISTRY.exists():
        return {"factors": {}}
    return json.loads(_REGISTRY.read_text(encoding="utf-8"))


def factor_status(name: str) -> str:
    reg = load_factor_registry().get("factors") or {}
    meta = reg.get(name) or {}
    return str(meta.get("status") or "unknown")


def production_factors() -> frozenset[str]:
    reg = load_factor_registry().get("factors") or {}
    return frozenset(k for k, v in reg.items() if isinstance(v, dict) and v.get("status") == "production")


def quarantine_factors() -> frozenset[str]:
    reg = load_factor_registry().get("factors") or {}
    return frozenset(k for k, v in reg.items() if isinstance(v, dict) and v.get("status") == "quarantine")


__all__ = ["factor_status", "load_factor_registry", "production_factors", "quarantine_factors"]

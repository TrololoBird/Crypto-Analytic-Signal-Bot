"""Compat shim — canonical defaults loader in domain.config (P11)."""
from __future__ import annotations

from hunt_core.domain.config import (
    load_config_defaults_toml,
    universal_section_from_defaults,
)

__all__ = ["load_config_defaults_toml", "universal_section_from_defaults"]

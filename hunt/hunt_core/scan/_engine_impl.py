"""Scanner compat facade — re-exports split modules (wave 3C)."""
from __future__ import annotations

from hunt_core.scan.routing import *  # noqa: F403

import hunt_core.scan._confirm_shared as _confirm_shared
import hunt_core.scan.early as _early
import hunt_core.scan.predump as _predump
import hunt_core.scan.predump_dump_hunt as _predump_dump_hunt
import hunt_core.scan.prepump as _prepump

for _mod in (_confirm_shared, _predump_dump_hunt, _predump, _prepump, _early):
    for _name, _val in vars(_mod).items():
        if not _name.startswith("__"):
            globals()[_name] = _val

__all__ = [n for n in globals() if not n.startswith("_")]

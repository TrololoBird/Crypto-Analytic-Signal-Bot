"""Lab lane shim → ``hunt_core._dev.expansion_lab.history``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.history')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['Any', 'ExpansionHistory', 'HistorySample', 'annotations', 'dataclass', 'deque', 'global_history', 'threading', 'time']

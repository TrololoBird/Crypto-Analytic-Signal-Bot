"""Lab lane shim → ``hunt_core._dev.expansion_lab.execution.engine``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.execution.engine')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['BlockContext', 'ExpansionExecution', 'annotations', 'build_execution', 'opt_float', 'safe_float']

"""Lab lane shim → ``hunt_core._dev.expansion_lab.blocks.funding``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.blocks.funding')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['BlockContext', 'BlockResult', 'NAME', 'abstain', 'annotations', 'clamp01', 'opt_float', 'result', 'safe_float', 'score']

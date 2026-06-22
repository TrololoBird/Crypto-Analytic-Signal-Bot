"""Lab lane shim → ``hunt_core._dev.expansion_lab.blocks.absorption``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.blocks.absorption')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['BlockContext', 'BlockResult', 'NAME', 'abstain', 'annotations', 'opt_float', 'result', 'score']

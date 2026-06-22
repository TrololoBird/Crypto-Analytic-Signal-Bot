"""Lab lane shim → ``hunt_core._dev.expansion_lab.blocks.market_maker_trap``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.blocks.market_maker_trap')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['BlockContext', 'BlockResult', 'NAME', 'abstain', 'annotations', 'clamp01', 'result', 'score']

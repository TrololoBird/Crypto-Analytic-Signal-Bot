"""Lab lane shim → ``hunt_core._dev.expansion_lab.blocks.wyckoff_signals``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.blocks.wyckoff_signals')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['BlockContext', 'BlockResult', 'abstain', 'annotations', 'clamp01', 'opt_float', 'result', 'score_sos', 'score_sow', 'score_spring', 'score_upthrust', 'structure_setup_type']

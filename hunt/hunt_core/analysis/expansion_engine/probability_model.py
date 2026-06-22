"""Lab lane shim → ``hunt_core._dev.expansion_lab.probability_model``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.probability_model')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['BlockDeltas', 'BlockResult', 'ExpansionConfig', 'ExpansionProbabilities', 'ExpansionProbabilityModel', 'annotations', 'clamp01', 'math']

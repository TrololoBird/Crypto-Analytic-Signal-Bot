"""Lab lane shim → ``hunt_core._dev.expansion_lab.ranking.opportunity_score``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.ranking.opportunity_score')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['annotations', 'clamp01', 'compute_opportunity_score']

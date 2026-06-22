"""Lab lane shim → ``hunt_core._dev.expansion_lab.rotation.engine``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.rotation.engine')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['ExpansionOpportunity', 'Iterable', 'annotations', 'clamp01', 'compute_rotation_scores', 'sector_rotation_score']

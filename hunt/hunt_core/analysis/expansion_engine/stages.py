"""Lab lane shim → ``hunt_core._dev.expansion_lab.stages``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.stages')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['BlockResult', 'ExpansionConfig', 'ExpansionProbabilities', 'ExpansionStateKind', 'annotations', 'classify_lifecycle_stage', 'derive_state']

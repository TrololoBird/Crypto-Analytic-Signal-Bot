"""Lab lane shim → ``hunt_core._dev.expansion_lab.quality``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.quality')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['BlockResult', 'ExpansionConfig', 'ExpansionProbabilities', 'Readiness', 'annotations', 'clamp01', 'expansion_quality', 'fake_breakout_risk', 'readiness', 'risk_label']

"""Lab lane shim → ``hunt_core._dev.expansion_lab.ranking.scan``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.ranking.scan')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['Any', 'ExpansionConfig', 'ExpansionOpportunity', 'Iterable', 'MetaScores', 'annotations', 'clamp01', 'compute_opportunity_score', 'compute_rotation_scores', 'load_expansion_config', 'rank_universe', 'replace']

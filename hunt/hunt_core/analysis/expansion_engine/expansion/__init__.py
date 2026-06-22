"""Lab lane shim → ``hunt_core._dev.expansion_lab``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['ExpansionConfig', 'ExpansionExecution', 'ExpansionForecast', 'ExpansionOpportunity', 'ExpansionProbabilities', 'annotations', 'blocks', 'build_expansion_dict', 'build_expansion_opportunity', 'compute_opportunity_score', 'config', 'deltas', 'execution', 'expansion', 'features', 'forecast', 'format', 'format_expansion_card', 'format_expansion_section', 'format_scan', 'history', 'load_expansion_config', 'opportunity_from_row', 'probability_model', 'quality', 'rank_universe', 'ranking', 'rotation', 'stages', 'state_machine', 'types']

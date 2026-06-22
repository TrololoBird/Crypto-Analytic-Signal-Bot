"""Lab lane shim → ``hunt_core._dev.expansion_lab.expansion.orchestrator``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.expansion.orchestrator')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['Any', 'BlockContext', 'BlockResult', 'ExpansionConfig', 'ExpansionHistory', 'ExpansionOpportunity', 'ExpansionProbabilityModel', 'ExpansionStateMachine', 'MetaScores', 'annotations', 'build_execution', 'build_expansion_opportunity', 'build_forecast', 'classify_lifecycle_stage', 'compute_deltas', 'compute_opportunity_score', 'derive_state', 'expansion_quality', 'fake_breakout_risk', 'global_history', 'global_state_machine', 'load_expansion_config', 'opportunity_from_row', 'persistence_block', 'readiness', 'risk_label', 'score_base_blocks', 'scores_obj_with', 'trigger_block']

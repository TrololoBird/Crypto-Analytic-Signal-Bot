"""Lab lane shim → ``hunt_core._dev.expansion_lab.format``."""
from __future__ import annotations
import importlib
_mod = importlib.import_module('hunt_core._dev.expansion_lab.format')
for _name in dir(_mod):
    if _name.startswith('_'):
        continue
    globals()[_name] = getattr(_mod, _name)
__all__ = ['Any', 'ExpansionOpportunity', 'annotations', 'format_calibration_report', 'format_expansion_card', 'format_expansion_section', 'format_expansion_section_from_dict', 'format_outcome_stats', 'format_review_summary', 'format_scan', 'format_universe_alert', 'html', 'serialize_opportunity']

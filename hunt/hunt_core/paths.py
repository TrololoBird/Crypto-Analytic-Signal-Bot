"""Canonical hunt data paths — all runtime state under hunt/data/."""
from __future__ import annotations



from pathlib import Path

# hunt/ (package parent)
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots"

WATCHLIST = DATA / "hunt_watchlist.json"
SIGNAL_STATE = DATA / "hunt_signal_state.json"
TELEGRAM_COOLDOWN = DATA / "dump_watch_telegram_state.json"
HUNT_SCAN_JSONL = DATA / "hunt_scan.jsonl"
DEEP_TICKS_JSONL = DATA / "deep_ticks.jsonl"
# Expansion Engine — advisory PRE-PUMP/PRE-DUMP scan + outcome learning ledger.
EXPANSION_SCAN_JSONL = DATA / "expansion_scan.jsonl"
EXPANSION_OUTCOMES_JSONL = DATA / "expansion_outcomes.jsonl"
EXPANSION_CALIBRATION_JSON = DATA / "expansion_calibration.json"
EXPANSION_ALERT_STATE = DATA / "expansion_alert_state.json"
EXPANSION_RUNTIME_STATE_JSON = DATA / "expansion_runtime_state.json"
# Legacy alias — writers use HUNT_SCAN_JSONL; readers fall back to dump_minute_watch.jsonl
TICK_JSONL = HUNT_SCAN_JSONL
LEGACY_TICK_JSONL = DATA / "dump_minute_watch.jsonl"
WATCH_LOG = DATA / "dump_minute_watch.log"
IGNITION_STATE = DATA / "hunt_ignition_state.json"
PUMP_HISTORY = DATA / "pump_history.json"
# EWMA tick stats only — scanner/ignition; never overwrite calibration.
EWMA_THRESHOLDS = DATA / "ewma_thresholds.json"
# Legacy combined file; migrated to EWMA + HUNT_CALIBRATION on watch startup.
ADAPTIVE_THRESHOLDS = DATA / "adaptive_thresholds.json"
HUNT_CALIBRATION = DATA / "hunt_calibration.json"
SESSION_DIR = DATA / "session"
SIGNAL_EVENTS = DATA / "signal_events.jsonl"
PREP_SHADOW_STATE = DATA / "prep_shadow_state.json"
PREP_SHADOW_EVENTS = DATA / "prep_shadow_events.jsonl"
SETUP_CANDIDATES_STATE = DATA / "setup_candidates_state.json"
SETUP_CANDIDATES_EVENTS = DATA / "setup_candidates.jsonl"
MARKET_REGIME = DATA / "market_regime.json"
# Persistent closed signal archive — append-only, never wiped, used for backtest/autotune
SIGNAL_HISTORY = DATA / "signal_history.jsonl"
SENT_MESSAGES = DATA / "sent_messages.jsonl"
BACKTEST_OUTCOMES = DATA / "backtest_outcomes.jsonl"
# ATR-enriched grade (realistic vol-based levels) — preferred truth source when present
BACKTEST_OUTCOMES_ENRICHED = DATA / "backtest_outcomes_enriched.jsonl"
# Confirmed-gate setups graded vs raw baseline — measures the confirmation edge
GATE_EDGE_OUTCOMES = DATA / "gate_edge_outcomes.jsonl"
INTEL_DOSSIER_MD = DATA / "intel_dossier.md"
INTEL_DOSSIER_JSON = DATA / "intel_dossier.json"
INTEL_REPORT = DATA / "intel_report.json"
DEEP_WATCH_GLOB = "deep_watch_*.jsonl"
DUMP_HUNT_ALERT_STATE = DATA / "dump_hunt_alert_state.json"

# Data lake (rewrite)
LAKE = DATA / "lake"
LAKE_DB = LAKE / "hunt_lake.sqlite"
LAKE_PARQUET = LAKE / "parquet"
MAPS_LAKE_JSONL = LAKE / "maps_bundles.jsonl"
VERDICT_V2_PATTERN_AUDIT_JSONL = DATA / "verdict_v2_patterns.jsonl"
VERDICT_V2_CALIBRATION_JSON = DATA / "verdict_v2_calibration.json"
VERDICT_V2_GATE_OVERRIDES_JSON = DATA / "verdict_v2_gate_overrides.json"
VERDICT_V2_SIGNAL_QUEUE_JSON = DATA / "verdict_v2_signal_queue.json"
UNIFIED_LABELS = DATA / "unified_labels.jsonl"
BASELINE_DIR = DATA / "baseline"

__all__ = [
    "ADAPTIVE_THRESHOLDS",
    "BACKTEST_OUTCOMES",
    "BACKTEST_OUTCOMES_ENRICHED",
    "BASELINE_DIR",
    "DATA",
    "DEEP_TICKS_JSONL",
    "DEEP_WATCH_GLOB",
    "EXPANSION_ALERT_STATE",
    "EXPANSION_CALIBRATION_JSON",
    "EXPANSION_OUTCOMES_JSONL",
    "EXPANSION_RUNTIME_STATE_JSON",
    "EXPANSION_SCAN_JSONL",
    "DUMP_HUNT_ALERT_STATE",
    "EWMA_THRESHOLDS",
    "GATE_EDGE_OUTCOMES",
    "HUNT_CALIBRATION",
    "HUNT_SCAN_JSONL",
    "LEGACY_TICK_JSONL",
    "IGNITION_STATE",
    "INTEL_DOSSIER_JSON",
    "INTEL_DOSSIER_MD",
    "INTEL_REPORT",
    "LAKE",
    "LAKE_DB",
    "LAKE_PARQUET",
    "MAPS_LAKE_JSONL",
    "MARKET_REGIME",
    "PREP_SHADOW_EVENTS",
    "PREP_SHADOW_STATE",
    "SETUP_CANDIDATES_EVENTS",
    "SETUP_CANDIDATES_STATE",
    "PUMP_HISTORY",
    "ROOT",
    "SESSION_DIR",
    "SIGNAL_EVENTS",
    "SIGNAL_HISTORY",
    "SENT_MESSAGES",
    "SIGNAL_STATE",
    "SNAPSHOTS",
    "TELEGRAM_COOLDOWN",
    "TICK_JSONL",
    "UNIFIED_LABELS",
    "VERDICT_V2_CALIBRATION_JSON",
    "VERDICT_V2_GATE_OVERRIDES_JSON",
    "VERDICT_V2_PATTERN_AUDIT_JSONL",
    "VERDICT_V2_SIGNAL_QUEUE_JSON",
    "WATCHLIST",
    "WATCH_LOG",
]

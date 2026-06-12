"""Runtime data paths — all hunt state lives under hunt/data/."""

from __future__ import annotations

from pathlib import Path

# hunt/ (package parent)
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots"

WATCHLIST = DATA / "hunt_watchlist.json"
SIGNAL_STATE = DATA / "hunt_signal_state.json"
TELEGRAM_COOLDOWN = DATA / "dump_watch_telegram_state.json"
TICK_JSONL = DATA / "dump_minute_watch.jsonl"
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
MARKET_REGIME = DATA / "market_regime.json"
# Persistent closed signal archive — append-only, never wiped, used for backtest/autotune
SIGNAL_HISTORY = DATA / "signal_history.jsonl"
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

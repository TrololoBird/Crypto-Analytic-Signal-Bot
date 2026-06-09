"""Runtime data paths — all hunt-watch state lives under hunt-watch/data/."""

from __future__ import annotations

from pathlib import Path

# hunt-watch/ (package parent)
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots"

WATCHLIST = DATA / "hunt_watchlist.json"
SIGNAL_STATE = DATA / "hunt_signal_state.json"
TELEGRAM_COOLDOWN = DATA / "dump_watch_telegram_state.json"
TICK_JSONL = DATA / "dump_minute_watch.jsonl"
WATCH_LOG = DATA / "dump_minute_watch.log"

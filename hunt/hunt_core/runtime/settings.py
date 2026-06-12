"""Hunter runtime constants and shared mutable state."""

from __future__ import annotations

from typing import Literal

from hunt_watch.deliver.sniper import SniperConfig
from hunt_watch.market_regime import REGIME_REFRESH_S
from hunt_watch.paths import TELEGRAM_COOLDOWN, TICK_JSONL
from hunt_watch.scriptutil import configure_script_logging
from hunt_watch.targets import DEFAULT_MODES, PINNED_SYMBOLS

WatchMode = Literal["short", "long", "both"]

SYMBOL_WATCH_MODES: dict[str, WatchMode] = dict(DEFAULT_MODES)
SYMBOL_TICK_TIMEOUT_S = 180

OUT_PATH = TICK_JSONL
STATE_PATH = TELEGRAM_COOLDOWN
SCAN_INTERVAL_S = 900
TICK_ROTATE_INTERVAL_S = 600
TICK_ROTATE_MIN_BYTES = 65_536
COOLDOWN_MINUTES = 45
FORMING_MIN_SCORE = 45
MIN_RISK_REWARD = 1.0
HUNT_MIN_RISK_REWARD = 0.8
BOUNCE_MIN_RISK_REWARD = 0.5

IGNITION_WINDOW_S = 300
IGNITION_MIN_PCT = 2.5
IGNITION_MIN_VOL_DELTA_USD = 250_000.0
IGNITION_MIN_QVOL_USD = 3_000_000.0
IGNITION_TTL_S = 7200.0
IGNITION_TELEGRAM_ENABLED = False

SQUEEZE_BB_PCTILE_MAX = 0.20
SQUEEZE_DONCHIAN_MAX_PCT = 8.0
SQUEEZE_MIN_VOL_24H_M = 5.0
SQUEEZE_COOLDOWN_MINUTES = 240

SNIPER_CONFIG = SniperConfig.from_env()
LOG = configure_script_logging("scripts.dump_minute_watch")
STOP = False


def request_stop() -> None:
    global STOP
    STOP = True

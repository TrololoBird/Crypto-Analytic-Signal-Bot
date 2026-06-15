#!/usr/bin/env python3
"""One-shot P2 split: collect.py -> ingest (collect) + tick_assembly + scanner + scoring."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "hunt_core"
COLLECT = ROOT / "data" / "collect.py"
lines = COLLECT.read_text(encoding="utf-8").splitlines(keepends=True)

# 0-indexed slice helpers (line numbers from rg audit)
SCORING_START = 1106  # squeeze_watch starts 1107 -> index 1106
SCORING_END = 1718    # through _confirm_long
SNAPSHOT_START = 1719  # snapshot_symbol
SNAPSHOT_END = 2276    # return result before __all__
INGEST_START = 2294    # --- merged from data/ingest/rest_pack.py ---
SCANNER_START = 2537   # --- merged from data/prescan.py ---
HEADER_END = 77        # through IMPULSE constants before rsi14

scoring_body = "".join(lines[SCORING_START:SCORING_END])
snapshot_body = "".join(lines[SNAPSHOT_START:SNAPSHOT_END])
helpers_body = "".join(lines[HEADER_END:SCORING_START])  # rsi14 through _tf_snapshot
ingest_body = "".join(lines[INGEST_START:SCANNER_START])
scanner_body = "".join(lines[SCANNER_START:])

scoring_py = f'''"""Dump/long scoring and squeeze advisory (extracted from collect P2)."""
from __future__ import annotations

import html
from typing import Any, Literal

from hunt_core.gate.delivery import directional_filters
from hunt_core.params.store import collect_thresholds, effective_hunt_params
from hunt_core.track.pump_history import score_bonus
from hunt_core.runtime.settings import merge_hunt_extremes
from hunt_core.detect.engine import (
    confirm_dump as _se_confirm_dump,
    confirm_long as _se_confirm_long,
    enrich_dump_setup,
    enrich_long_setup,
    phase_dump as _se_phase_dump,
    phase_long as _se_phase_long,
    wall_depth_fuel_triggers,
)

from hunt_core.runtime.tick_assembly import (
    WatchMode,
    _col,
    _distribution_stats,
    _hidden_stoch_divergence,
    _lite_prepared,
    _session_stats,
    _stale_15m_flag,
    _tf_snapshot,
    _tf_snapshot_for_symbol,
    squeeze_watch as _squeeze_watch_impl,
)

{scoring_body}
'''

# Fix: scoring imports squeeze_watch from itself - use direct body
scoring_py = f'''"""Dump/long scoring and squeeze advisory (extracted from collect P2)."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.gate.delivery import directional_filters
from hunt_core.params.store import collect_thresholds, effective_hunt_params
from hunt_core.track.pump_history import score_bonus
from hunt_core.runtime.settings import merge_hunt_extremes
from hunt_core.detect.engine import (
    confirm_dump as _se_confirm_dump,
    confirm_long as _se_confirm_long,
    enrich_dump_setup,
    enrich_long_setup,
    phase_dump as _se_phase_dump,
    phase_long as _se_phase_long,
    wall_depth_fuel_triggers,
)

{scoring_body}
'''

tick_assembly_py = f'''"""Full tick assembly: prepare + lifecycle + detect (P2 boundary — not data/ingest)."""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime
from typing import Any, Literal

import polars as pl

LOG = logging.getLogger("hunt_core.runtime.tick_assembly")

from hunt_core.data.completeness import (
    REQUIRED_SIGNAL_KLINE_TFS,
    audit_kline_integrity,
    repair_kline_map_gaps,
    series_z_strict,
)
from hunt_core.data.collect import (
    SnapshotTier,
    _apply_rest_enrichments,
    _book_from_pack,
    _fetch_rest_pack,
    _kline_integrity_reject,
    _overlay_ws_market,
    kline_limits,
    resolve_kline_map,
    safe_fetch,
    ws_orderflow_fresh,
)
from hunt_core.gate.delivery import liquidity_skip_reason
from hunt_core.features.prepare import prepare_symbol
from hunt_core.features.prepare_columns import resolve_prepare_groups_for_symbol
from hunt_core.features.levels import fib_retracement_levels, structural_long_levels, structural_short_levels
from hunt_core.features.levels import build_liquidity_context
from hunt_core.detect.lifecycle import (
    apply_short_invalidation,
    assess_hunt_lifecycle,
    attach_regime,
    effective_support_break,
    lifecycle_to_dict,
    stabilize as stabilize_lifecycle,
)
from hunt_core.detect.engine import detect_pp, wall_depth_fuel_triggers
from hunt_core.detect.scoring import (
    _confirm_dump,
    _confirm_long,
    _dump_analysis,
    _long_analysis,
    _phase,
    _phase_long,
    format_squeeze_telegram,
    squeeze_watch,
)
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.market import HuntCcxtClient, HuntCcxtSpotCompanion, HuntCcxtStreams
from hunt_core.market.live_price import resolve_live_price
from hunt_core.data_readiness import assess_symbol_data_readiness
from hunt_core.domain.schemas import SymbolFrames, UniverseSymbol
from hunt_core.errors import DEFENSIVE_EXC
from hunt_core.features.candle_patterns import candle_pattern_snapshot
from hunt_core.features.chart_patterns import chart_pattern_snapshot
from hunt_core.features.pivots import _pivot_rows, rsi_trendline_break, with_spec_columns
from hunt_core.analysis.pinned_deep import enrich_pinned_tf_snapshot, prepare_htf_frame
from hunt_core.analysis.trend_engine import legacy_trend_label, trend_from_snapshot
from hunt_core.features.research_plugins import enrich_research_columns, research_snapshot_fields
from hunt_core.domain.market_regime import symbol_regime_features
from hunt_core.runtime.settings import SymbolStateStore
from hunt_core.market.client import normalize_depth_levels
from hunt_core.params.store import collect_thresholds

WatchMode = Literal["short", "long", "both"]

IMPULSE_WINDOW: dict[str, int] = {{
    "BTCUSDT": 30,
    "ETHUSDT": 30,
    "XAUUSDT": 24,
    "XAGUSDT": 24,
}}
IMPULSE_WINDOW_1H: dict[str, int] = {{
    "BTCUSDT": 168,
    "ETHUSDT": 120,
    "XAUUSDT": 72,
    "XAGUSDT": 72,
}}
IMPULSE_WINDOW_ALT_4H = 12
IMPULSE_WINDOW_ALT_1H = 48

{helpers_body}

{snapshot_body}

__all__ = [
    "WatchMode",
    "snapshot_symbol",
    "format_squeeze_telegram",
    "squeeze_watch",
]
'''

collect_header = '''"""Per-symbol data ingest — REST/WS fetch only (P2 boundary)."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime
from typing import Any, Literal

import polars as pl

LOG = logging.getLogger("hunt_core.data.collect")

from hunt_core.data.completeness import (
    DataIncompleteError,
    REQUIRED_SIGNAL_KLINE_TFS,
    audit_kline_integrity,
    repair_kline_map_gaps,
    series_z_strict,
)
from hunt_core.features.prepare_columns import book_walls_from_depth
from hunt_core.features.prepare_columns import patch_work_4h, should_use_young_lite_path
from hunt_core.features.polars_ta_bridge import rsi_series as _rsi_series
from hunt_core.market import HuntCcxtClient, HuntCcxtStreams
from hunt_core.data_readiness import kline_fetch_limit
from hunt_core.domain.schemas import SymbolFrames, UniverseSymbol
from hunt_core.errors import DEFENSIVE_EXC
from hunt_core.features.prepare import _prepare_frame
from hunt_core.market.client import depth_imbalance_from_book, microprice_bias_from_book
from hunt_core.market.client import normalize_depth_levels

'''

collect_helpers = "".join(lines[110:125])  # kline_limits start
# Add safe_fetch and ingest helpers from original - lines 200-680 approx for _book, _apply_rest, etc.
collect_helpers += "".join(lines[199:680])
collect_helpers += ingest_body

collect_py = collect_header + collect_helpers + '''
__all__ = [
    "SnapshotTier",
    "TickBatchCache",
    "safe_fetch",
    "kline_limits",
    "fetch_rest_pack",
    "resolve_kline_map",
    "rest_pack_specs",
    "ws_orderflow_fresh",
    "sort_symbols_for_tick",
    "refresh_tick_batch_cache",
]
'''

scanner_py = f'''"""Universe prescan and hunt scanner (extracted from collect P2)."""
from __future__ import annotations

{scanner_body}
'''

# Write files
(ROOT / "detect" / "scoring.py").write_text(scoring_py, encoding="utf-8")
(ROOT / "runtime" / "tick_assembly.py").write_text(tick_assembly_py, encoding="utf-8")
(ROOT / "data" / "scanner.py").write_text(scanner_py, encoding="utf-8")
COLLECT.write_text(collect_py, encoding="utf-8")
print("split done")

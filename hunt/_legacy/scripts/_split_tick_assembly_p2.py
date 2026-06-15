#!/usr/bin/env python3
"""P2 split: tick_assembly → features/snapshot.py + detect/scoring.py."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "hunt_core"
SRC = ROOT / "runtime" / "tick_assembly.py"
lines = SRC.read_text(encoding="utf-8").splitlines(keepends=True)

SNAPSHOT_HEADER = '''"""TF / market / regime snapshot builders (P2 extract from tick_assembly)."""
from __future__ import annotations

import asyncio
import html
import logging
import math
from datetime import UTC, datetime
from typing import Any, Literal

import polars as pl

from hunt_core.data.collect import (
    _apply_rest_enrichments,
    _book_from_pack,
    _kline_integrity_reject,
    _overlay_ws_market,
    kline_limits,
    safe_fetch,
)
from hunt_core.data.completeness import series_z_strict
from hunt_core.features.candle_patterns import candle_pattern_snapshot
from hunt_core.features.chart_patterns import chart_pattern_snapshot
from hunt_core.features.pivots import _pivot_rows, rsi_trendline_break, with_spec_columns
from hunt_core.features.polars_ta_bridge import rsi_series as _rsi_series
from hunt_core.features.research_plugins import enrich_research_columns, research_snapshot_fields
from hunt_core.analysis.pinned_deep import enrich_pinned_tf_snapshot, prepare_htf_frame
from hunt_core.analysis.trend_engine import legacy_trend_label, trend_from_snapshot
from hunt_core.domain.market_regime import symbol_regime_features
from hunt_core.detect.engine import detect_pp
from hunt_core.errors import DEFENSIVE_EXC
from hunt_core.market.client import depth_imbalance_from_book, microprice_bias_from_book

LOG = logging.getLogger("hunt_core.features.snapshot")

WatchMode = Literal["short", "long", "both"]

'''

SCORING_HEADER = '''"""Dump/long scoring + confirm wrappers (P2 extract from tick_assembly)."""
from __future__ import annotations

from typing import Any

from hunt_core.detect.engine import (
    confirm_dump as _se_confirm_dump,
    confirm_long as _se_confirm_long,
    enrich_dump_setup,
    enrich_long_setup,
    phase_dump as _se_phase_dump,
    phase_long as _se_phase_long,
    wall_depth_fuel_triggers,
)
from hunt_core.data.collect import SnapshotTier
from hunt_core.features.levels import (
    build_liquidity_context,
    fib_retracement_levels,
    structural_long_levels,
    structural_short_levels,
)
from hunt_core.gate.delivery import directional_filters
from hunt_core.params.store import collect_thresholds, effective_hunt_params
from hunt_core.track.pump_history import score_bonus

'''

TICK_HEADER = '''"""Full tick assembly orchestration (P2 — snapshot + scoring + lifecycle)."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from hunt_core.data.collect import (
    SnapshotTier,
    _fetch_rest_pack,
    kline_limits,
    resolve_kline_map,
    safe_fetch,
    ws_orderflow_fresh,
)
from hunt_core.data.completeness import (
    REQUIRED_SIGNAL_KLINE_TFS,
    audit_kline_integrity,
    repair_kline_map_gaps,
)
from hunt_core.detect.scoring import (
    confirm_dump as _confirm_dump,
    confirm_long as _confirm_long,
    dump_analysis as _dump_analysis,
    long_analysis as _long_analysis,
    phase_dump as _phase,
    phase_long as _phase_long,
)
from hunt_core.detect.engine import enrich_dump_setup, enrich_long_setup
from hunt_core.detect.lifecycle import (
    apply_short_invalidation,
    assess_hunt_lifecycle,
    attach_regime,
    effective_support_break,
    lifecycle_to_dict,
    stabilize as stabilize_lifecycle,
)
from hunt_core.features.prepare import _prepare_frame, prepare_symbol
from hunt_core.features.prepare_columns import (
    book_walls_from_depth,
    patch_work_4h,
    resolve_prepare_groups_for_symbol,
    should_use_young_lite_path,
)
from hunt_core.features.snapshot import (
    WatchMode,
    apply_cross_exchange_flat,
    apply_rest_enrichments_local,
    attach_cross_market_fields,
    attach_pp_flags,
    attach_research_setup_fields,
    btc_beta_1h,
    btc_corr_1h,
    col as _col,
    data_quality_report,
    distribution_stats,
    enrich_work_research_frames,
    format_squeeze_telegram,
    impulse_context,
    kline_integrity_reject,
    lite_prepared,
    market_snapshot,
    merge_research_tf_fields,
    merge_ws_kline_closed,
    regime_snapshot,
    session_stats,
    squeeze_watch,
    tf_snapshot,
    tf_snapshot_for_symbol,
    tf_snapshot_lite,
)
from hunt_core.gate.delivery import liquidity_skip_reason
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.data_readiness import assess_symbol_data_readiness, kline_fetch_limit
from hunt_core.domain.schemas import SymbolFrames, UniverseSymbol
from hunt_core.features.levels import fib_retracement_levels
from hunt_core.market import HuntCcxtClient, HuntCcxtSpotCompanion, HuntCcxtStreams
from hunt_core.market.client import normalize_depth_levels
from hunt_core.market.live_price import resolve_live_price
from hunt_core.runtime.settings import SymbolStateStore, merge_hunt_extremes

LOG = logging.getLogger("hunt_core.runtime.tick_assembly")

# Backward-compat re-exports
kline_limits = kline_limits
safe_fetch = safe_fetch
squeeze_watch = squeeze_watch
format_squeeze_telegram = format_squeeze_telegram

'''

# Line numbers are 1-based in editor; slice 91-1139 = index 90:1139
snapshot_body = lines[90:1139]
scoring_body = lines[1143:1730]
orchestration_body = lines[1732:]  # snapshot_symbol onwards

# Rename private functions in snapshot for public API where needed
snapshot_text = SNAPSHOT_HEADER + "".join(snapshot_body)
snapshot_text = snapshot_text.replace("def _kline_integrity_reject", "def kline_integrity_reject")
snapshot_text = snapshot_text.replace("def _lite_prepared", "def lite_prepared")
snapshot_text = snapshot_text.replace("def _apply_cross_exchange_flat", "def apply_cross_exchange_flat")
snapshot_text = snapshot_text.replace("async def _attach_cross_market_fields", "async def attach_cross_market_fields")
snapshot_text = snapshot_text.replace("def _enrich_work_research_frames", "def enrich_work_research_frames")
snapshot_text = snapshot_text.replace("def _merge_research_tf_fields", "def merge_research_tf_fields")
snapshot_text = snapshot_text.replace("def _attach_research_setup_fields", "def attach_research_setup_fields")
snapshot_text = snapshot_text.replace("def _apply_rest_enrichments", "def apply_rest_enrichments_local")
snapshot_text = snapshot_text.replace("def _overlay_ws_market", "def _overlay_ws_market")  # keep private
snapshot_text = snapshot_text.replace("def _market_snapshot", "def market_snapshot")
snapshot_text = snapshot_text.replace("def _regime_snapshot", "def regime_snapshot")
snapshot_text = snapshot_text.replace("def _data_quality_report", "def data_quality_report")
snapshot_text = snapshot_text.replace("def _col(", "def col(")
snapshot_text = snapshot_text.replace("def _merge_ws_kline_closed", "def merge_ws_kline_closed")
snapshot_text = snapshot_text.replace("def _tf_snapshot_lite", "def tf_snapshot_lite")
snapshot_text = snapshot_text.replace("def _tf_snapshot_for_symbol", "def tf_snapshot_for_symbol")
snapshot_text = snapshot_text.replace("def _attach_pp_flags", "def attach_pp_flags")
snapshot_text = snapshot_text.replace("def _tf_snapshot(", "def tf_snapshot(")
snapshot_text = snapshot_text.replace("def _impulse_context", "def impulse_context")
snapshot_text = snapshot_text.replace("def _session_stats", "def session_stats")
snapshot_text = snapshot_text.replace("def _distribution_stats", "def distribution_stats")
snapshot_text = snapshot_text.replace("def _btc_corr_1h", "def btc_corr_1h")
snapshot_text = snapshot_text.replace("def _btc_beta_1h", "def btc_beta_1h")

scoring_text = SCORING_HEADER + "".join(scoring_body)
scoring_text = scoring_text.replace("def _dump_analysis", "def dump_analysis")
scoring_text = scoring_text.replace("def _confirm_dump", "def confirm_dump")
scoring_text = scoring_text.replace("def _phase(", "def phase_dump(")
scoring_text = scoring_text.replace("def _phase_long", "def phase_long")
scoring_text = scoring_text.replace("def _long_analysis", "def long_analysis")

orchestration_text = TICK_HEADER + "".join(orchestration_body)
# Fix internal references in orchestration
replacements = {
    "_kline_integrity_reject": "kline_integrity_reject",
    "_lite_prepared": "lite_prepared",
    "_apply_cross_exchange_flat": "apply_cross_exchange_flat",
    "_attach_cross_market_fields": "attach_cross_market_fields",
    "_enrich_work_research_frames": "enrich_work_research_frames",
    "_merge_research_tf_fields": "merge_research_tf_fields",
    "_attach_research_setup_fields": "attach_research_setup_fields",
    "_market_snapshot": "market_snapshot",
    "_regime_snapshot": "regime_snapshot",
    "_data_quality_report": "data_quality_report",
    "_merge_ws_kline_closed": "merge_ws_kline_closed",
    "_tf_snapshot_for_symbol": "tf_snapshot_for_symbol",
    "_tf_snapshot_lite": "tf_snapshot_lite",
    "_tf_snapshot": "tf_snapshot",
    "_impulse_context": "impulse_context",
    "_session_stats": "session_stats",
    "_distribution_stats": "distribution_stats",
    "_btc_corr_1h": "btc_corr_1h",
    "_btc_beta_1h": "btc_beta_1h",
    "_book_from_pack": "_book_from_pack",
}
for old, new in replacements.items():
    orchestration_text = orchestration_text.replace(old, new)

# _book_from_pack still from collect
orchestration_text = orchestration_text.replace(
    "from hunt_core.data.collect import (\n    SnapshotTier,\n    _fetch_rest_pack,\n    kline_limits,\n    resolve_kline_map,\n    safe_fetch,\n    ws_orderflow_fresh,\n)",
    "from hunt_core.data.collect import (\n    SnapshotTier,\n    _book_from_pack,\n    _fetch_rest_pack,\n    kline_limits,\n    resolve_kline_map,\n    safe_fetch,\n    ws_orderflow_fresh,\n)",
)

(ROOT / "features" / "snapshot.py").write_text(snapshot_text, encoding="utf-8")
(ROOT / "detect" / "scoring.py").write_text(scoring_text, encoding="utf-8")
SRC.write_text(orchestration_text, encoding="utf-8")
print("Wrote snapshot.py, scoring.py, slim tick_assembly.py")

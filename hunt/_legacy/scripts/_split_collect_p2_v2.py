#!/usr/bin/env python3
"""P2 split v2 — preserve working imports."""
from __future__ import annotations

from pathlib import Path

SRC = Path("/tmp/collect_backup.py")
lines = SRC.read_text(encoding="utf-8").splitlines(keepends=True)

INGEST_START = 2294   # line 2295
SCANNER_START = 2537  # line 2538
BODY_END = 2277       # through snapshot return + blank

ingest_section = "".join(lines[INGEST_START:SCANNER_START])
scanner_section = "".join(lines[SCANNER_START:])
body_section = "".join(lines[0:BODY_END])

COLLECT = Path(__file__).resolve().parents[1] / "hunt_core" / "data" / "collect.py"
TICK = Path(__file__).resolve().parents[1] / "hunt_core" / "runtime" / "tick_assembly.py"
SCANNER = Path(__file__).resolve().parents[1] / "hunt_core" / "data" / "scanner.py"

collect_py = f'''"""REST/WS data ingest (P2 — no detect/analysis imports)."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Literal

import polars as pl

LOG = logging.getLogger("hunt_core.data.collect")

from hunt_core.data.completeness import (
    REQUIRED_SIGNAL_KLINE_TFS,
    audit_kline_integrity,
    repair_kline_map_gaps,
)
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.data_readiness import kline_fetch_limit
from hunt_core.domain.schemas import SymbolFrames, UniverseSymbol
from hunt_core.errors import DEFENSIVE_EXC
from hunt_core.features.prepare import _prepare_frame
from hunt_core.market import HuntCcxtClient, HuntCcxtStreams
from hunt_core.market.client import normalize_depth_levels

{"".join(lines[110:148])}
{"".join(lines[199:211])}
{"".join(lines[310:598])}
{ingest_section}

_fetch_rest_pack = fetch_rest_pack

__all__ = [
    "SnapshotTier",
    "TickBatchCache",
    "safe_fetch",
    "kline_limits",
    "fetch_rest_pack",
    "_fetch_rest_pack",
    "resolve_kline_map",
    "rest_pack_specs",
    "ws_orderflow_fresh",
    "sort_symbols_for_tick",
    "refresh_tick_batch_cache",
    "_book_from_pack",
    "_apply_rest_enrichments",
    "_overlay_ws_market",
    "_kline_integrity_reject",
]
'''

# tick_assembly: original body, replace snapshot imports from collect
tick_py = body_section.replace(
    "from hunt_core.data.collect import safe_fetch, snapshot_symbol",
    "",
)
# Add collect ingest imports after LOG line
insert_after = 'LOG = logging.getLogger("hunt_core.data.collect")\n'
tick_py = tick_py.replace(
    insert_after,
    insert_after
    + """
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
""",
    1,
)
tick_py = tick_py.replace(
    'LOG = logging.getLogger("hunt_core.data.collect")',
    'LOG = logging.getLogger("hunt_core.runtime.tick_assembly")',
    1,
)
tick_py = tick_py.replace(
    '"""Per-symbol tick collection — REST/WS snapshot assembly (H-B rewrite)."""',
    '"""Full tick assembly: prepare + lifecycle + detect (P2)."""',
    1,
)

scanner_py = f'"""Universe prescan and hunt scanner (P2)."""\nfrom __future__ import annotations\n\n{scanner_section}'

COLLECT.write_text(collect_py, encoding="utf-8")
TICK.write_text(tick_py, encoding="utf-8")
SCANNER.write_text(scanner_py, encoding="utf-8")
print("split v2 done", len(collect_py.splitlines()), len(tick_py.splitlines()))

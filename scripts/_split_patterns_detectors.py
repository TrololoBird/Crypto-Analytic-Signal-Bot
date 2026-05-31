"""One-off: split patterns.py into per-detector modules. Run from repo root."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ROOT / "bot" / "setups" / "detectors" / "patterns.py"
OUT_DIR = ROOT / "bot" / "setups" / "detectors"

# (module_name, primary public function name)
MODULES: list[tuple[str, str]] = [
    ("fvg", "detect_fvg"),
    ("bos_choch", "detect_bos_choch"),
    ("structure_break_retest", "detect_structure_break_retest"),
    ("structure_pullback", "detect_structure_pullback"),
    ("ob", "detect_order_block"),
    ("breaker_block", "detect_breaker_block"),
    ("liquidity_sweep", "detect_liquidity_sweep"),
    ("turtle_soup", "detect_turtle_soup"),
    ("stop_hunt", "detect_stop_hunt"),
    ("wyckoff_spring", "detect_wyckoff_spring"),
    ("wick_trap", "detect_wick_trap"),
    ("volume_anomaly", "detect_volume_anomaly"),
    ("volume_climax", "detect_volume_climax_reversal"),
    ("ema_bounce", "detect_ema_bounce"),
    ("keltner_breakout", "detect_keltner_breakout"),
    ("atr_expansion", "detect_atr_expansion"),
    ("bb_squeeze", "detect_bb_squeeze_release"),
    ("price_velocity", "detect_price_velocity"),
    ("vwap", "detect_vwap_reclaim"),
    ("aggression_shift", "detect_aggression_shift"),
    ("absorption", "detect_absorption"),
    ("indicator_divergence", "detect_regular_divergence"),
    ("hidden_divergence", "detect_hidden_divergence"),
    ("cvd_divergence", "detect_cvd_divergence"),
]

COMMON_HEADER = '''"""Shared spec detector primitives."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import polars as pl

from ...domain.config import BotSettings
from ...domain.schemas import PreparedSymbol, Signal
from ...features import _swing_points
from ...features.shared import wilder_mean
from ...domain.catalog_guards import catalog_allows_signal
from ...domain.strategy_catalog import catalog_default_params
from .. import _build_signal, _compute_dynamic_score, _reject

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpecHit:
    strategy: str
    direction: str
    entry: float
    stop_basis: float
    atr: float
    timeframe: str
    reasons: tuple[str, ...]
    structure_clarity: float = 0.6
    vol_ratio: float = 1.0
    rsi: float = 50.0
    source_index: int | None = None


'''

DETECTOR_HEADER = '''"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import (
    SpecHit,
    as_float,
    build_spec_signal,
    finite_or_none,
    required_columns,
    with_spec_columns,
    _latest_values,
    _pivot_rows,
    _row_volume_ratio,
    _clean_impulse,
    _valid_order_block_rows,
    current_utc_hour,
)

__all__ = [
'''

def main() -> None:
    text = PATTERNS.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    # Find function boundaries
    func_starts: dict[str, int] = {}
    for i, line in enumerate(lines):
        m = re.match(r"^def (\w+)\(", line)
        if m:
            func_starts[m.group(1)] = i

    # Common block: from start through end of _pivot_rows (inclusive)
    common_end = func_starts["detect_fvg"]
    common_body = "".join(lines[:common_end])
    # Strip duplicate imports from common - keep from line 8 onwards inside file
    common_body = common_body.split('LOGGER = logging.getLogger(__name__)\n\n\n', 1)[-1]
    (OUT_DIR / "_common.py").write_text(COMMON_HEADER + common_body, encoding="utf-8")

    # Assign helpers to modules
    helper_owner: dict[str, str] = {
        "_row_volume_ratio": "fvg",
        "_clean_impulse": "structure_break_retest",
        "_valid_order_block_rows": "ob",
        "current_utc_hour": "session_killzone",
    }

    ranges: dict[str, tuple[int, int]] = {}
    ordered_funcs = sorted(func_starts.items(), key=lambda x: x[1])
    for idx, (name, start) in enumerate(ordered_funcs):
        if name.startswith("_") and name not in helper_owner:
            continue
        if not name.startswith("detect_") and name != "current_utc_hour":
            continue
        end = ordered_funcs[idx + 1][1] if idx + 1 < len(ordered_funcs) else len(lines)
        owner = helper_owner.get(name)
        if owner:
            ranges.setdefault(owner, [start, end])
            ranges[owner][1] = max(ranges[owner][1], end)
        else:
            for mod, pub in MODULES:
                if pub == name:
                    ranges[mod] = [start, end]
                    break

    # Attach private helpers to their owners
    for helper, mod in helper_owner.items():
        if helper in func_starts:
            h_start = func_starts[helper]
            h_end = h_start + 1
            for fname, fstart in ordered_funcs:
                if fstart > h_start:
                    h_end = fstart
                    break
            else:
                h_end = len(lines)
            if mod in ranges:
                ranges[mod][0] = min(ranges[mod][0], h_start)
                ranges[mod][1] = max(ranges[mod][1], h_end)
            else:
                ranges[mod] = [h_start, h_end]

    exports: list[str] = []
    for mod, pub in MODULES:
        if mod not in ranges:
            print(f"WARN: no range for {mod}")
            continue
        start, end = ranges[mod]
        body = "".join(lines[start:end])
        pub_names = [pub]
        if mod == "ob":
            pub_names.append("_valid_order_block_rows")
        content = DETECTOR_HEADER.replace("__all__ = [", f'__all__ = ["{pub}",\n')
        if "_valid_order_block_rows" in body and mod == "ob":
            content = content.replace('__all__ = ["detect_order_block",\n', '__all__ = ["detect_order_block", "_valid_order_block_rows",\n')
        content += body + "\n"
        (OUT_DIR / f"{mod}.py").write_text(content, encoding="utf-8")
        exports.append(f'from .{mod} import {pub}')
        print(f"Wrote {mod}.py ({end - start} lines)")

    # patterns.py shim
    shim = '''"""Backward-compatible re-exports — prefer bot.setups.detectors.<module>."""
from __future__ import annotations

from ._common import (
    SpecHit,
    as_float,
    build_spec_signal,
    finite_or_none,
    required_columns,
    with_spec_columns,
    _latest_values,
    _pivot_rows,
)
from .fvg import detect_fvg
from .bos_choch import detect_bos_choch
from .structure_break_retest import detect_structure_break_retest
from .structure_pullback import detect_structure_pullback
from .ob import detect_order_block
from .breaker_block import detect_breaker_block
from .liquidity_sweep import detect_liquidity_sweep
from .turtle_soup import detect_turtle_soup
from .stop_hunt import detect_stop_hunt
from .wyckoff_spring import detect_wyckoff_spring
from .wick_trap import detect_wick_trap
from .volume_anomaly import detect_volume_anomaly
from .volume_climax import detect_volume_climax_reversal
from .ema_bounce import detect_ema_bounce
from .keltner_breakout import detect_keltner_breakout
from .atr_expansion import detect_atr_expansion
from .bb_squeeze import detect_bb_squeeze_release
from .price_velocity import detect_price_velocity
from .vwap import detect_vwap_reclaim
from .aggression_shift import detect_aggression_shift
from .absorption import detect_absorption
from .indicator_divergence import detect_regular_divergence
from .hidden_divergence import detect_hidden_divergence
from .cvd_divergence import detect_cvd_divergence
from ._common import current_utc_hour

__all__ = [
    "SpecHit",
    "as_float",
    "build_spec_signal",
    "finite_or_none",
    "required_columns",
    "with_spec_columns",
    "detect_fvg",
    "detect_bos_choch",
    "detect_structure_break_retest",
    "detect_structure_pullback",
    "detect_order_block",
    "detect_breaker_block",
    "detect_liquidity_sweep",
    "detect_turtle_soup",
    "detect_stop_hunt",
    "detect_wyckoff_spring",
    "detect_wick_trap",
    "detect_volume_anomaly",
    "detect_volume_climax_reversal",
    "detect_ema_bounce",
    "detect_keltner_breakout",
    "detect_atr_expansion",
    "detect_bb_squeeze_release",
    "detect_price_velocity",
    "detect_vwap_reclaim",
    "detect_aggression_shift",
    "detect_absorption",
    "detect_regular_divergence",
    "detect_hidden_divergence",
    "detect_cvd_divergence",
    "current_utc_hour",
]
'''
    backup = PATTERNS.with_suffix(".py.bak")
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")
    PATTERNS.write_text(shim, encoding="utf-8")
    print("Updated patterns.py shim; backup at patterns.py.bak")


if __name__ == "__main__":
    main()

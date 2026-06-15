"""One-shot wave 3C scanner split — predump/prepump/_confirm_shared/early."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "hunt_core" / "scan"
DUMP = (ROOT / "_dump_core.py").read_text(encoding="utf-8").splitlines(keepends=True)
ENGINE = (ROOT / "_engine_impl.py").read_text(encoding="utf-8").splitlines(keepends=True)

HEADER = '''"""{doc}"""
from __future__ import annotations

import html
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

from hunt_core.data.universe import watchlist_flags
from hunt_core.domain.market_regime import HuntCalibratedParams
from hunt_core.params.store import (
    confirm_thresholds,
    dump_fast_confirm_enabled,
    effective_hunt_params,
    entry_confirm_tf,
    liquidation_thresholds,
    listings_thresholds,
    orderflow_thresholds,
    scoring_thresholds,
)
from hunt_core.paths import ADAPTIVE_THRESHOLDS, DUMP_HUNT_ALERT_STATE, EWMA_THRESHOLDS, IGNITION_STATE
from hunt_core.errors import optional_finite_float, require_mark_price


def _htf_bias_override(*args, **kwargs):
    from hunt_core.regime.leg_fsm import htf_bias_override
    return htf_bias_override(*args, **kwargs)


'''

SCORE_HELPERS = '''
_TRIGGER_REASONS = frozenset({
    "1m_macd_cross_down",
    "1m_macd_hist_neg",
    "1m_macd_exhaust",
    "below_support",
    "hunt_short_confirmed",
})

_SETUP_REASON_PREFIXES = (
    "1h_rsi=",
    "15m_rsi=",
    "top_ls=",
    "funding_crowded=",
    "phase=",
)


def _fval(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _market_val(market: dict[str, Any], micro: dict[str, Any], key: str) -> Any:
    val = market.get(key)
    if val is not None:
        return val
    return micro.get(key)


def _setup_hits(reasons: list[str]) -> int:
    return sum(1 for r in reasons if r.startswith(_SETUP_REASON_PREFIXES))


def _has_trigger(reasons: list[str]) -> bool:
    return any(
        r in _TRIGGER_REASONS
        or r.startswith("fall_trigger=")
        or r.startswith("below_support=")
        for r in reasons
    )


'''


def slice_lines(lines: list[str], start: int, end: int) -> str:
    """1-based inclusive start/end."""
    return "".join(lines[start - 1 : end])


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    print(f"wrote {path.name} ({len(content.splitlines())} lines)")


# _confirm_shared: fuel cluster + confirm helpers (511-1620)
shared_body = slice_lines(DUMP, 511, 1620)
shared = (
    HEADER.format(doc="Shared confirm/fuel helpers (wave 3C).")
    + "from hunt_core.scan.predump_dump_hunt import DumpHuntTier\n\n"
    + shared_body
)
write(ROOT / "_confirm_shared.py", shared)

# predump_dump_hunt: score helpers + dump hunt state/telegram (helpers through tier_from_verdict + state fns)
# Lines 37-467 in original (score_dump_init through format_dump_hunt) + 469-509 maybe_send
predump_hunt = (
    HEADER.format(doc="Dump-hunt tier state + telegram (wave 3C).")
    + SCORE_HELPERS
    + slice_lines(DUMP, 37, 467)
    + "\n"
    + slice_lines(DUMP, 469, 509)
    + "\n"
)
write(ROOT / "predump_dump_hunt.py", predump_hunt)

# predump: confirm_dump, phase_dump, enrich_dump + evaluate_predump
predump_main = (
    HEADER.format(doc="Pre-dump scanner path (§4.1 — CONFIRM short cascade).")
    + "from hunt_core.scan._confirm_shared import *\n"  # noqa: F403
    + "from hunt_core.scan.predump_dump_hunt import (\n"
    + "    DumpHuntTier,\n"
    + "    dump_hunt_cooldown_ok,\n"
    + "    dump_hunt_skip_reason,\n"
    + "    format_dump_hunt_telegram,\n"
    + "    mark_dump_hunt_sent,\n"
    + "    maybe_send_dump_hunt_telegram,\n"
    + "    score_dump_init,\n"
    + "    tier_from_verdict,\n"
    + "    display_short_setup,\n"
    + ")\n\n"
    + slice_lines(DUMP, 1622, 1825)
    + "\n"
    + slice_lines(DUMP, 1970, 1994)
    + "\n"
    + slice_lines(DUMP, 2012, 2042)
    + "\n\n"
    + '''def evaluate_predump(row: dict[str, Any], *, price: float, tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    dump = dict(row.get("dump") or {})
    dump = enrich_dump_setup(dump, price=price, tf=tf, market=market)
    sym = str(row.get("symbol") or "")
    cal = effective_hunt_params(sym)
    confirmed, _hard = confirm_dump(dump, tf=tf, market=market, symbol=sym, price=price, cal=cal)
    dump["confirmed"] = confirmed
    dump["phase"] = phase_dump(dump, confirmed, symbol=sym)
    return dump


__all__ = [
    "DumpHuntTier",
    "confirm_dump",
    "display_short_setup",
    "dump_hunt_cooldown_ok",
    "dump_hunt_skip_reason",
    "enrich_dump_setup",
    "evaluate_predump",
    "format_dump_hunt_telegram",
    "mark_dump_hunt_sent",
    "maybe_send_dump_hunt_telegram",
    "phase_dump",
    "score_dump_init",
    "tier_from_verdict",
]
'''
)
write(ROOT / "predump.py", predump_main)

# prepump
prepump = (
    HEADER.format(doc="Pre-pump scanner path (§4.2 — long bounce / squeeze-up).")
    + "from hunt_core.scan._confirm_shared import *\n"  # noqa: F403
    + "\n"
    + slice_lines(DUMP, 1826, 1969)
    + "\n"
    + slice_lines(DUMP, 1995, 2011)
    + "\n"
    + slice_lines(DUMP, 2043, 2090)
    + "\n\n"
    + '''def evaluate_prepump(row: dict[str, Any], *, price: float, tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    long = dict(row.get("long") or {})
    long = enrich_long_setup(long, price=price, tf=tf, market=market)
    sym = str(row.get("symbol") or "")
    cal = effective_hunt_params(sym)
    confirmed, _hard = confirm_long(long, tf=tf, market=market, symbol=sym, price=price, cal=cal)
    long["confirmed"] = confirmed
    long["phase"] = phase_long(long, confirmed, cal=cal, symbol=sym)
    return long


__all__ = ["confirm_long", "enrich_long_setup", "evaluate_prepump", "phase_long"]
'''
)
write(ROOT / "prepump.py", prepump)

# early.py from engine (skip header and dump_core import)
early_start = None
for i, line in enumerate(ENGINE):
    if line.startswith("EWMA_ALPHA"):
        early_start = i
        break
if early_start is None:
    raise SystemExit("EWMA_ALPHA not found in _engine_impl")

early_header = '''"""Adaptive thresholds, ignition, and early alerts (wave 3C)."""
from __future__ import annotations

import html
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

from hunt_core.data.universe import watchlist_flags
from hunt_core.params.store import effective_hunt_params
from hunt_core.paths import ADAPTIVE_THRESHOLDS, EWMA_THRESHOLDS, IGNITION_STATE
from hunt_core.features.structure import detect_pp

'''
early_body = "".join(ENGINE[early_start:])
# trim trailing __all__ block from engine if present
if "__all__" in early_body:
    early_body = early_body.split("__all__")[0].rstrip() + "\n"
write(ROOT / "early.py", early_header + early_body)

# thin _engine_impl shim
shim = '''"""Scanner compat facade — re-exports split modules (wave 3C)."""
from __future__ import annotations

from hunt_core.scan._confirm_shared import *  # noqa: F403
from hunt_core.scan.predump import *  # noqa: F403
from hunt_core.scan.predump_dump_hunt import *  # noqa: F403
from hunt_core.scan.prepump import *  # noqa: F403
from hunt_core.scan.early import *  # noqa: F403
from hunt_core.scan.routing import *  # noqa: F403

__all__ = [n for n in dir() if not n.startswith("_")]
'''
write(ROOT / "_engine_impl.py", shim)

# remove _dump_core
(ROOT / "_dump_core.py").unlink()
print("deleted _dump_core.py")

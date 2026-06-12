"""H-B edge policy — per-direction TG promotion gates."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunt_core.paths import GATE_EDGE_OUTCOMES

LONG_SL_GATE = 0.35
LONG_TP1_GATE = 0.25
LONG_MIN_N = 30
SHORT_SL_BASELINE = 0.30


@dataclass(frozen=True, slots=True)
class EdgePolicyConfig:
    wide_hunter: bool = True
    long_tg_enabled: bool = False
    long_sl_max: float = LONG_SL_GATE
    long_tp1_min: float = LONG_TP1_GATE
    long_min_n: int = LONG_MIN_N

    @classmethod
    def from_env(cls) -> EdgePolicyConfig:
        wide = os.environ.get("HUNT_WIDE_MODE", "1") not in {"0", "false", "False"}
        long_on = os.environ.get("HUNT_LONG_TG", "0") in {"1", "true", "True"}
        return cls(wide_hunter=wide, long_tg_enabled=long_on)


def _load_gate_edge_long_stats(path: Path | None = None) -> dict[str, Any]:
    p = path or GATE_EDGE_OUTCOMES
    if not p.exists():
        return {"n": 0, "sl_rate": None, "tp1_plus_rate": None}
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("direction") == "long":
            rows.append(row)
    n = len(rows)
    if n == 0:
        return {"n": 0, "sl_rate": None, "tp1_plus_rate": None}
    sl = sum(1 for r in rows if r.get("bt_outcome") == "sl_hit")
    tp1p = sum(1 for r in rows if r.get("bt_outcome") in ("tp1_hit", "tp2_hit"))
    return {"n": n, "sl_rate": sl / n, "tp1_plus_rate": tp1p / n}


def long_tg_allowed(config: EdgePolicyConfig | None = None) -> tuple[bool, str]:
    """Return (allowed, reason) for long Telegram delivery."""
    cfg = config or EdgePolicyConfig.from_env()
    if not cfg.wide_hunter:
        return False, "wide_mode_off"
    if cfg.long_tg_enabled:
        return True, "env_override"
    stats = _load_gate_edge_long_stats()
    n = int(stats["n"])
    if n < cfg.long_min_n:
        return False, f"long_n_below_{cfg.long_min_n}"
    sl = stats.get("sl_rate")
    tp1p = stats.get("tp1_plus_rate")
    if sl is None or sl > cfg.long_sl_max:
        return False, f"long_sl_{sl:.2f}" if sl is not None else "long_sl_unknown"
    if tp1p is None or tp1p < cfg.long_tp1_min:
        return False, f"long_tp1_{tp1p:.2f}" if tp1p is not None else "long_tp1_unknown"
    return True, "edge_gate_pass"


def direction_block_reason(
    direction: str,
    *,
    config: EdgePolicyConfig | None = None,
) -> str | None:
    """Machine block code if direction vetoed by H-B edge policy."""
    cfg = config or EdgePolicyConfig.from_env()
    if direction == "long":
        ok, reason = long_tg_allowed(cfg)
        if not ok:
            return f"hb_long_{reason}"
    return None

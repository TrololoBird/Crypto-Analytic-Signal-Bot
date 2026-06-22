"""Block 15 — Cycle context (same signal, different market phase).

A pre-pump setup in a bull regime or early altseason is worth more than the identical
setup in a bear-dominant tape. Reads the regime classifier output + BTC context as a
context coefficient, not a standalone direction.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import clamp01, safe_float
from hunt_core._dev.expansion_lab.blocks._common import abstain, result
from hunt_core._dev.expansion_lab.types import BlockContext, BlockResult

NAME = "cycle_context"

_BULLISH = {"bull", "trend_up", "uptrend", "markup", "expansion"}
_BEARISH = {"bear", "trend_down", "downtrend", "markdown"}


def score(ctx: BlockContext) -> BlockResult:
    r = ctx.regime
    regime = str(r.get("market_regime") or r.get("regime_4h") or "").lower()
    btc_ctx = ctx.row.get("btc_context") if isinstance(ctx.row.get("btc_context"), dict) else {}
    btc_regime = str(btc_ctx.get("regime") or btc_ctx.get("trend") or "").lower()
    btc_chg = safe_float(btc_ctx.get("chg_24h_pct"))
    alt_chg = safe_float(ctx.row.get("chg_24h_pct"))

    if not regime and not btc_regime:
        return abstain(NAME)

    evidence: list[str] = []
    up = down = 0.0
    if any(b in regime for b in _BULLISH):
        up += 0.4
        evidence.append(f"regime={regime}")
    elif any(b in regime for b in _BEARISH):
        down += 0.4
        evidence.append(f"regime={regime}")

    # Altseason proxy: BTC roughly flat while the alt is outperforming.
    if not ctx.symbol.startswith("BTC") and abs(btc_chg) < 2.0 and alt_chg > 3.0:
        up += 0.35
        evidence.append("altseason_proxy")
    if any(b in btc_regime for b in _BEARISH) and ctx.symbol.startswith("BTC"):
        down += 0.2

    if up == 0.0 and down == 0.0:
        return result(NAME, 0.35, direction="neutral", evidence=("regime_neutral",))
    if up >= down:
        return result(NAME, clamp01(0.4 + up), direction="up", evidence=tuple(evidence))
    return result(NAME, clamp01(0.4 + down), direction="down", evidence=tuple(evidence))


__all__ = ["NAME", "score"]

"""Block 7 — Relative strength vs BTC.

Alt holding (or rising) while BTC is flat/falling is a classic pre-pump tell; the mirror
(alt weak vs a strong BTC) leans pre-dump. Uses the residual-decoupling flags + beta.
"""
from __future__ import annotations

from hunt_core.analysis.expansion_engine._util import clamp01, opt_float, safe_float
from hunt_core.analysis.expansion_engine.blocks._common import abstain, result
from hunt_core.analysis.expansion_engine.types import BlockContext, BlockResult

NAME = "strength"


def score(ctx: BlockContext) -> BlockResult:
    sym = ctx.symbol
    if sym.startswith("BTC"):
        return abstain(NAME)
    r = ctx.regime
    decoupled_up = bool(r.get("btc_decoupled_pump"))
    decoupled_down = bool(r.get("btc_decoupled_dump"))
    beta = opt_float(r.get("btc_beta_1h"))
    corr = opt_float(r.get("btc_corr_1h"))
    btc_ctx = ctx.row.get("btc_context") if isinstance(ctx.row.get("btc_context"), dict) else {}
    btc_chg = safe_float(btc_ctx.get("chg_24h_pct"))
    alt_chg = safe_float(ctx.row.get("chg_24h_pct"))

    evidence: list[str] = []
    if not decoupled_up and not decoupled_down and beta is None and corr is None:
        return abstain(NAME)

    up = down = 0.0
    if decoupled_up:
        up += 0.6
        evidence.append("btc_decoupled_pump")
    if decoupled_down:
        down += 0.6
        evidence.append("btc_decoupled_dump")
    # Alt outperforming a flat/weak BTC.
    rs = alt_chg - btc_chg
    if abs(rs) >= 2.0:
        if rs > 0:
            up += clamp01(rs / 15.0) * 0.4
            evidence.append(f"rs_vs_btc=+{rs:.1f}%")
        else:
            down += clamp01(abs(rs) / 15.0) * 0.4
            evidence.append(f"rs_vs_btc={rs:.1f}%")

    if up == 0.0 and down == 0.0:
        return abstain(NAME)
    if up >= down:
        return result(NAME, up, direction="up", evidence=tuple(evidence))
    return result(NAME, down, direction="down", evidence=tuple(evidence))


__all__ = ["NAME", "score"]

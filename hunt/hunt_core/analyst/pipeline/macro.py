from __future__ import annotations

from typing import Any

from hunt_core.analyst.pipeline._helpers import safe_float
from hunt_core.analyst.pipeline.config import MacroConfig
from hunt_core.analyst.pipeline.macro_data import fetch_macro_data
from hunt_core.analyst.pipeline.types import MacroContext, ModuleResult


def run_macro_filter(
    row: dict[str, Any],
    cfg: MacroConfig,
    direction: str = "long",
    exchange: Any = None,
) -> tuple[ModuleResult, MacroContext]:
    btc_ctx = row.get("btc_context") or {}
    sym = str(row.get("symbol") or "").upper()
    is_btc = sym == "BTCUSDT"
    sym_is_alt = not is_btc

    evidence: list[str] = []
    bearish_signals: list[str] = []
    bullish_signals: list[str] = []

    macro_snap = fetch_macro_data(
        api_key=cfg.cmc_api_key,
        base_url=cfg.cmc_base_url,
        max_age=cfg.cmc_cache_ttl,
    )

    btc_above_ema = btc_ctx.get("btc_above_ema50")
    ema_label = "EMA50"
    if btc_above_ema is None:
        btc_above_ema = btc_ctx.get("btc_above_ema200")
        ema_label = "EMA200"

    btc_price = safe_float(btc_ctx.get("btc_price"))
    btc_chg_24h = safe_float(btc_ctx.get("chg_24h_pct") or btc_ctx.get("btc_chg_24h_pct"))
    btc_atr_pct = safe_float(btc_ctx.get("atr_pct"))

    if btc_above_ema is not None:
        if btc_above_ema:
            evidence.append(f"btc_above_{ema_label.lower()}")
            bullish_signals.append(f"BTC>{ema_label}")
        else:
            evidence.append(f"btc_below_{ema_label.lower()}")
            bearish_signals.append(f"BTC<{ema_label}")
    else:
        evidence.append("btc_ema_unknown")

    btc_d_chg = macro_snap.btc_d_change_24h
    if btc_d_chg is not None:
        if btc_d_chg > cfg.btc_dominance_block_threshold:
            evidence.append(f"btc_dominance_up_{btc_d_chg:+.1f}%")
            bearish_signals.append(f"BTC.D+{btc_d_chg:+.1f}%")
        elif btc_d_chg < -cfg.btc_dominance_block_threshold:
            evidence.append(f"btc_dominance_down_{btc_d_chg:+.1f}%")
            bullish_signals.append(f"BTC.D{btc_d_chg:+.1f}%")
        else:
            evidence.append(f"btc_dominance_{btc_d_chg:+.1f}%")
    else:
        evidence.append("btc_dominance_unknown")
        if not bearish_signals:
            evidence.append("macro_api_unavailable")

    total3_chg = macro_snap.total3_change_24h
    if total3_chg is not None:
        if total3_chg < -cfg.total3_drop_block_threshold:
            evidence.append(f"total3_dropping_{total3_chg:+.1f}%")
            bearish_signals.append(f"TOTAL3{total3_chg:+.1f}%")
        elif total3_chg > cfg.total3_drop_block_threshold:
            evidence.append(f"total3_growing_{total3_chg:+.1f}%")
            bullish_signals.append(f"TOTAL3+{total3_chg:+.1f}%")
        else:
            evidence.append(f"total3_{total3_chg:+.1f}%")
    else:
        evidence.append("total3_unknown")

    macro_context = MacroContext(
        btc_above_ema200=btc_above_ema200,
        btc_price=btc_price,
        btc_chg_24h=btc_chg_24h,
        btc_atr_pct=btc_atr_pct,
        btc_d=macro_snap.btc_d,
        btc_d_change_24h=btc_d_chg,
        total3_cap=macro_snap.total3_cap,
        total3_change_24h=total3_chg,
    )

    api_available = btc_d_chg is not None or total3_chg is not None

    if direction == "long":
        if bearish_signals and sym_is_alt:
            if not api_available and btc_above_ema is None:
                return ModuleResult(
                    status="CAUTION",
                    reason="; ".join(bearish_signals) + " (данные макро недоступны)",
                    details={"evidence": evidence, "bearish": bearish_signals, "direction": direction, "api_available": False},
                ), macro_context
            return ModuleResult(
                status="FAIL",
                reason="; ".join(bearish_signals),
                details={"evidence": evidence, "bearish": bearish_signals, "direction": direction},
            ), macro_context
        if not api_available and btc_above_ema is None:
            return ModuleResult(
                status="CAUTION",
                reason="Нет данных макро (BTC.D/TOTAL3 недоступны)",
                details={"evidence": evidence, "direction": direction, "api_available": False},
            ), macro_context
        return ModuleResult(
            status="PASS",
            reason="Макро-фон благоприятный",
            details={"evidence": evidence, "direction": direction},
        ), macro_context

    if bearish_signals and sym_is_alt:
        return ModuleResult(
            status="PASS",
            reason="Макро-фон медвежий — благоприятно для шорта",
            details={"evidence": evidence, "bearish": bearish_signals, "direction": direction},
        ), macro_context
    return ModuleResult(
        status="PASS",
        reason="Макро-фон благоприятный",
        details={"evidence": evidence, "direction": direction},
    ), macro_context

from __future__ import annotations

import html
from typing import Any

from hunt_core.analyst.pipeline.types import FiveModuleResult, MacroContext, MarketRegime, ModuleResult


REGIME_EMOJI = {
    MarketRegime.NORMAL: "📊",
    MarketRegime.HIGH_VOL: "🌋",
    MarketRegime.CRASH: "💥",
    MarketRegime.ALT_SEASON: "🚀",
}

REGIME_LABEL = {
    MarketRegime.NORMAL: "Normal",
    MarketRegime.HIGH_VOL: "High Vol",
    MarketRegime.CRASH: "Crash",
    MarketRegime.ALT_SEASON: "Alt Season",
}


def _module_emoji(status: str) -> str:
    return {"PASS": "✅", "FAIL": "❌", "CAUTION": "⚠️", "UNKNOWN": "❓"}.get(status, "❓")


def _module_line(name: str, result: ModuleResult) -> str:
    emoji = _module_emoji(result.status)
    return f"├─ {emoji} <b>{name}</b> [{result.status}] <i>{html.escape(result.reason)}</i>"


def format_five_module_analytics(result: FiveModuleResult) -> str:
    gating_emoji = {"SIGNAL": "🔒", "REJECT": "🔒", "CAUTION": "⚠️"}.get(result.gating, "🔒")

    lines = [
        "📋 <b>АНАЛИТИКА [5 Модулей]:</b>",
        _module_line("Macro", result.macro),
        _module_line("Trend", result.trend),
        _module_line("Structure", result.structure),
        _module_line("Positioning", result.positioning),
        _module_line("Risk", result.risk),
    ]

    parts = [result.macro.status, result.trend.status, result.structure.status, result.positioning.status, result.risk.status]
    gating_str = "→".join(parts)

    if result.gating == "SIGNAL":
        lines.append(f"{gating_emoji} <b>GATING:</b> {gating_str} = <b>СИГНАЛ ОПУБЛИКОВАН</b>")
    elif result.gating == "CAUTION":
        lines.append(f"{gating_emoji} <b>GATING:</b> {gating_str} = <b>СИГНАЛ (sizing 0.5%)</b>")
    else:
        lines.append(f"{gating_emoji} <b>GATING:</b> {gating_str} = <b>REJECT</b>")

    regime_emoji = REGIME_EMOJI.get(result.regime, "📊")
    regime_label = REGIME_LABEL.get(result.regime, "Unknown")
    lines.append(f"{regime_emoji} <b>Режим:</b> {regime_label}")

    if result.positioning.status == "UNKNOWN":
        lines.append("⚠️ Positioning=UNKNOWN: сигнал только на Trend+Structure")

    rl = result.risk_levels
    if rl is not None and rl.rr_tp1 > 0:
        levels_line = (
            f"🎯 <b>Уровни:</b> SL <code>{_px(rl.stop_loss)}</code> · "
            f"TP1 <code>{_px(rl.tp1)}</code> ({rl.rr_tp1}R)"
        )
        if rl.tp2 is not None:
            levels_line += f" · TP2 <code>{_px(rl.tp2)}</code>"
        if rl.tp3 is not None:
            levels_line += f" · TP3 <code>{_px(rl.tp3)}</code>"
        lines.append(levels_line)
        sizing_label = f"{rl.sizing_modifier*100:.0f}%"
        lines.append(f"📏 ATR={rl.atr_pct:.2f}% · Размер: {sizing_label} · TTL={rl.ttl_hours:.0f}ч")

    return "\n".join(lines)


def _px(v: float) -> str:
    if v >= 10000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def format_macro_context(result: FiveModuleResult) -> str:
    mc = result.macro_context
    if mc is None:
        return ""

    parts = []
    if mc.btc_above_ema200 is not None:
        parts.append("BTC>EMA200" if mc.btc_above_ema200 else "BTC<EMA200")
    if getattr(mc, "btc_above_ema50", None) is not None:
        parts.append("BTC>EMA50" if mc.btc_above_ema50 else "BTC<EMA50")
    if mc.btc_chg_24h is not None:
        arrow = "↑" if mc.btc_chg_24h > 0 else "↓"
        parts.append(f"BTC{arrow}{mc.btc_chg_24h:+.1f}%")
    if mc.btc_d_change_24h is not None:
        arrow = "↑" if mc.btc_d_change_24h > 0 else "↓"
        parts.append(f"BTC.D{arrow}{mc.btc_d_change_24h:+.1f}%")
    elif mc.btc_d is not None:
        parts.append(f"BTC.D={mc.btc_d:.1f}%")
    if mc.total3_change_24h is not None:
        arrow = "↑" if mc.total3_change_24h > 0 else "↓"
        parts.append(f"TOTAL3{arrow}{mc.total3_change_24h:+.1f}%")

    if parts:
        return " · ".join(parts)
    return ""


def format_signal_header(result: FiveModuleResult, symbol: str, price: float) -> str:
    emoji_map = {"SIGNAL": "🟢" if result.direction == "long" else "🔴", "CAUTION": "🟡", "REJECT": "⏳"}
    emoji = emoji_map.get(result.gating, "⏳")
    dir_ru = "ЛОНГ" if result.direction == "long" else "ШОРТ" if result.direction == "short" else "ОЖИДАНИЕ"

    macro_line = format_macro_context(result)
    header = f"{emoji} <b>{symbol} · {dir_ru}</b>"
    if macro_line:
        header += f" · {macro_line}"
    if price > 0:
        header += f" · <code>{_px(price)}</code>"
    return header

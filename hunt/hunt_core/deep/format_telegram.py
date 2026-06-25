"""Telegram formatting for deep analysis (structure-first — no watch hunter narrative)."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.deep.build import DeepAnalysis, build_deep_report
from hunt_core.deliver._labels import fmt_price


def _expansion_text(row: dict[str, Any]) -> str:
    """Expansion Engine section — rendered separately from the Verdict block."""
    exp = row.get("expansion")
    if not isinstance(exp, dict) or not exp:
        return ""
    try:
        from hunt_core.expansion.format import format_expansion_section_from_dict

        return format_expansion_section_from_dict(exp)
    except Exception:
        return ""


def format_deep_analysis_telegram(analysis: DeepAnalysis) -> str:
    sym = html.escape(analysis.symbol.replace("USDT", "-USDT"))
    price = float(analysis.row.get("price") or 0)
    header = f"🔬 <b>Глубокий анализ</b> — <code>{sym}</code>"
    if price > 0:
        header += f" · <code>{fmt_price(price)}</code>"

    parts: list[str] = [header]
    as_of = analysis.row.get("as_of") or (analysis.row.get("freshness") or {}).get("as_of")
    if as_of:
        parts.append(f"<i>снимок: {html.escape(str(as_of)[:19].replace('T', ' '))} UTC</i>")
    v2_txt = analysis.verdict_v2_text()
    if v2_txt:
        parts.extend(["", v2_txt])
    mtf_txt = analysis.mtf_text()
    if mtf_txt:
        parts.extend(["", mtf_txt])
    exp_txt = _expansion_text(analysis.row)
    if exp_txt:
        parts.extend(["", exp_txt])
    fc_txt = analysis.forecast_text()
    if fc_txt:
        parts.extend(["", fc_txt])
    if analysis.include_watch_appendix:
        parts.extend(["", "<i>Статус сканера — справочно (только PRE-автоскан)</i>"])
        wd = "сигнал прошёл бы" if analysis.would_deliver else "сигнал НЕ прошёл бы"
        parts.append(f"<i>{wd}</i>")
        if analysis.blockers:
            bl = ", ".join(html.escape(str(b)) for b in analysis.blockers[:5])
            parts.append(f"<i>блокеры: {bl}</i>")
    parts.append("")
    parts.append("<i>Структура / МТФ / карты · вход вручную · не инвестрекомендация</i>")
    return "\n".join(parts)


def format_deep_from_row(row: dict[str, Any], **kwargs: Any) -> str:
    return format_deep_analysis_telegram(build_deep_report(row, **kwargs))


__all__ = ["format_deep_analysis_telegram", "format_deep_from_row"]

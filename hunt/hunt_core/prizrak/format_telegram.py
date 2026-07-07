"""Telegram formatting for analyst report (structure-first — no watch hunter narrative)."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.prizrak.build import AnalystReport, build_analyst_report_from_row
from hunt_core.deliver._labels import fmt_price


def format_analyst_telegram(analysis: AnalystReport) -> str:
    sym = html.escape(analysis.symbol.replace("USDT", "-USDT"))
    price = float(analysis.row.get("price") or 0)
    header = f"🔬 <b>Глубокий анализ</b> — <code>{sym}</code>"
    if price > 0:
        header += f" · <code>{fmt_price(price)}</code>"

    parts: list[str] = [header]
    as_of = analysis.row.get("as_of") or (analysis.row.get("freshness") or {}).get("as_of")
    if as_of:
        parts.append(f"<i>снимок: {html.escape(str(as_of)[:19].replace('T', ' '))} UTC</i>")

    v2_txt = analysis.prizrak_text()
    if v2_txt:
        parts.extend(["", v2_txt])
    # МТФ structure — the exact multi-scale read that gated the signal (single source).
    mtf_txt = analysis.mtf_text()
    if mtf_txt:
        parts.extend(["", mtf_txt])
    # Skip structural forecasts for WAIT signals — irrelevant if no trade
    row_v2 = analysis.row.get("prizrak_summary") or {}
    forecast_ok = str(row_v2.get("action") or "wait").strip().upper() in {"LONG", "SHORT"}
    if forecast_ok:
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


def format_analyst_from_row(row: dict[str, Any], **kwargs: Any) -> str:
    return format_analyst_telegram(build_analyst_report_from_row(row, **kwargs))


format_deep_analysis_telegram = format_analyst_telegram  # backward compat after deep→analyst rename

__all__ = ["format_analyst_telegram", "format_analyst_from_row", "format_deep_analysis_telegram"]

"""Telegram formatting for deep analysis (structure-first — no watch hunter narrative)."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.analysis.deep.build import DeepAnalysis, build_deep_report
from hunt_core.deliver._labels import fmt_price


def format_deep_analysis_telegram(analysis: DeepAnalysis) -> str:
    sym = html.escape(analysis.symbol.replace("USDT", "-USDT"))
    price = float(analysis.row.get("price") or 0)
    header = f"🔬 <b>Deep analysis</b> — <code>{sym}</code>"
    if price > 0:
        header += f" · <code>{fmt_price(price)}</code>"

    parts: list[str] = [header]
    mtf_txt = analysis.mtf_text()
    if mtf_txt:
        parts.extend(["", mtf_txt])
    panel_txt = analysis.indicator_panel_text()
    if panel_txt:
        parts.extend(["", panel_txt])
    verdict_txt = analysis.verdicts_text()
    if verdict_txt:
        parts.extend(["", verdict_txt])
    fc_txt = analysis.forecast_text()
    if fc_txt:
        parts.extend(["", fc_txt])
    if analysis.include_watch_appendix:
        parts.extend(["", "<i>Watch hunter status — appendix only (PRE-only auto-scan)</i>"])
        wd = "would deliver" if analysis.would_deliver else "would NOT deliver"
        parts.append(f"<i>{wd}</i>")
        if analysis.blockers:
            bl = ", ".join(html.escape(str(b)) for b in analysis.blockers[:5])
            parts.append(f"<i>blockers: {bl}</i>")
    parts.append("")
    parts.append("<i>Structure / MTF / maps · manual entry · not financial advice</i>")
    return "\n".join(parts)


def format_deep_from_row(row: dict[str, Any], **kwargs: Any) -> str:
    return format_deep_analysis_telegram(build_deep_report(row, **kwargs))


__all__ = ["format_deep_analysis_telegram", "format_deep_from_row"]

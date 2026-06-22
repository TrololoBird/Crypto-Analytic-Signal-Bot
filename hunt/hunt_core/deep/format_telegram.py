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
        from hunt_core.analysis.expansion_engine.format import format_expansion_section_from_dict

        return format_expansion_section_from_dict(exp)
    except Exception:
        return ""


def format_deep_analysis_telegram(analysis: DeepAnalysis) -> str:
    sym = html.escape(analysis.symbol.replace("USDT", "-USDT"))
    price = float(analysis.row.get("price") or 0)
    header = f"🔬 <b>Deep analysis</b> — <code>{sym}</code>"
    if price > 0:
        header += f" · <code>{fmt_price(price)}</code>"

    parts: list[str] = [header]
    v2_txt = analysis.verdict_v2_text()
    if v2_txt:
        parts.extend(["", v2_txt])
    mtf_txt = analysis.mtf_text()
    if mtf_txt:
        parts.extend(["", mtf_txt])
    verdict_txt = analysis.verdicts_text()
    if verdict_txt:
        parts.extend(["", verdict_txt])
    exp_txt = _expansion_text(analysis.row)
    if exp_txt:
        parts.extend(["", exp_txt])
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

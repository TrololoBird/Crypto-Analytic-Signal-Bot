"""Deep analysis orchestrator."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hunt_core.analysis.deep.forecast_panel import build_structural_forecast_panel
from hunt_core.analysis.deep.verdicts import build_three_verdicts
import html


@dataclass(frozen=True, slots=True)
class DeepAnalysis:
    symbol: str
    row: dict[str, Any]
    fusion: dict[str, Any]
    verdicts: dict[str, Any]
    forecasts: dict[str, dict[str, Any] | None]
    would_deliver: bool
    blockers: tuple[str, ...] = field(default_factory=tuple)
    pinned_verdict: Any | None = None
    include_watch_appendix: bool = True

    def fusion_text(self) -> str:
        return ""

    def forecast_text(self) -> str:
        return build_structural_forecast_panel(self.forecasts, self.row)

    def mtf_text(self) -> str:
        mtf = self.row.get("mtf")
        if mtf is None:
            return ""
        lines = ["📐 <b>MTF structure</b>"]
        tf_signals = getattr(mtf, "tf_signals", None) or {}
        for tf_key in ("1w", "1d", "4h", "1h", "15m", "5m"):
            sig = tf_signals.get(tf_key)
            if sig is None:
                continue
            trend = getattr(sig, "trend", None) or (
                sig.get("trend") if isinstance(sig, dict) else None
            )
            if trend:
                lines.append(f"  {tf_key}: <b>{html.escape(str(trend))}</b>")
        long_s = getattr(mtf, "long_scenario", None)
        short_s = getattr(mtf, "short_scenario", None)
        if long_s and short_s:
            lines.append(
                f"  long {float(getattr(long_s, 'score', 0)):.2f} "
                f"({getattr(long_s, 'htf_count', 0)}/{getattr(long_s, 'htf_total', 0)} HTF) · "
                f"short {float(getattr(short_s, 'score', 0)):.2f} "
                f"({getattr(short_s, 'htf_count', 0)}/{getattr(short_s, 'htf_total', 0)} HTF)"
            )
        dom = getattr(mtf, "dominant", None)
        if dom:
            lines.append(f"  dominant: <b>{html.escape(str(dom))}</b>")
        return "\n".join(lines) if len(lines) > 1 else ""

    def verdicts_text(self) -> str:
        v = self.verdicts
        lines = ["📊 <b>Verdicts</b> (structure-first)"]
        for key in ("long", "short", "sideways"):
            block = v.get(key) or {}
            score = block.get("score", 0)
            conf = block.get("confidence", 0)
            lines.append(f"  {key}: score <code>{score:.2f}</code> conf <code>{conf:.0%}</code>")
        dom = v.get("dominant") or "neutral"
        lines.append(f"Dominant: <b>{dom}</b>")
        reason = v.get("reason")
        if reason:
            clean = _sanitize_deep_reason(str(reason))
            if clean:
                lines.append(f"<i>{html.escape(clean)}</i>")
        src = v.get("source")
        if src:
            lines.append(f"<i>source: {src}</i>")
        return "\n".join(lines)

    def indicator_panel_text(self) -> str:
        panel = self.row.get("indicator_panel")
        if panel is None:
            return ""
        dominant = getattr(panel, "dominant", None) or (panel.get("dominant") if isinstance(panel, dict) else None)
        votes = getattr(panel, "total_votes", None) or (panel.get("total_votes") if isinstance(panel, dict) else 0)
        if not dominant:
            return ""
        return f"📈 Indicators: <b>{dominant}</b> ({votes} votes)"


def _enrich_deep_row(work: dict[str, Any]) -> dict[str, Any]:
    sym = str(work.get("symbol") or "").upper()
    tf = work.get("timeframes") or {}
    price = float(work.get("price") or 0)
    if not sym or not tf or price <= 0:
        return work

    from hunt_core.analysis.pinned_deep import (
        build_pinned_indicator_panel,
        build_pinned_verdict,
    )

    if not work.get("indicator_panel"):
        work["indicator_panel"] = build_pinned_indicator_panel(sym, tf)
    if not work.get("mtf"):
        from hunt_core.confluence.mtf import build_mtf_confluence

        work["mtf"] = build_mtf_confluence(
            sym,
            tf,
            price,
            market=work.get("market"),
            indicator_panel=work.get("indicator_panel"),
            row=work,
        )
    if not work.get("pinned_verdict"):
        work["pinned_verdict"] = build_pinned_verdict(work)
    return work


def build_deep_report(
    row: dict[str, Any],
    *,
    full: bool = True,
    include_watch_appendix: bool = True,
    would_deliver: bool | None = None,
    blockers: list[str] | None = None,
) -> DeepAnalysis:
    """Deep product path — pinned/MTF/maps; watch delivery is optional appendix."""
    sym = str(row.get("symbol") or "").upper()
    work = dict(row)
    if full:
        work = _enrich_deep_row(work)

    from hunt_core.maps.forecast import build_maps_forecast, build_dump_forecast

    up_fc = build_maps_forecast(work)
    down_fc = build_dump_forecast(work)
    forecasts = {
        "structural_up": up_fc,
        "structural_down": down_fc,
    }
    fusion = work.get("manipulation_fusion") if isinstance(work.get("manipulation_fusion"), dict) else {}

    verdicts = build_three_verdicts(work, fusion=fusion)
    pv = work.get("pinned_verdict")

    wd = would_deliver
    if wd is None and include_watch_appendix:
        wd = bool(work.get("would_deliver"))
    elif not include_watch_appendix:
        wd = False

    bl = tuple(blockers or work.get("delivery_blockers") or [])

    return DeepAnalysis(
        symbol=sym,
        row=work,
        fusion=fusion,
        verdicts=verdicts,
        forecasts=forecasts,
        would_deliver=bool(wd) if wd is not None else False,
        blockers=bl,
        pinned_verdict=pv,
        include_watch_appendix=include_watch_appendix,
    )


def build_deep_analysis(
    row: dict[str, Any],
    *,
    full: bool = True,
    would_deliver: bool | None = None,
    blockers: list[str] | None = None,
) -> DeepAnalysis:
    """Back-compat alias — prefer ``build_deep_report``."""
    return build_deep_report(
        row,
        full=full,
        include_watch_appendix=True,
        would_deliver=would_deliver,
        blockers=blockers,
    )


def _sanitize_deep_reason(reason: str) -> str:
    """Strip watch-hunter lifecycle / phase clauses from deep narrative."""
    skip = (
        "phase=",
        "hunt closed",
        "dump_",
        "long_setup",
        "dump_setup",
        "pre-dump",
        "pre-pump",
        "impulse",
    )
    parts = [
        p.strip()
        for p in reason.split(" · ")
        if p.strip() and not any(s in p.lower() for s in skip)
    ]
    return " · ".join(parts[:4])


__all__ = ["DeepAnalysis", "build_deep_analysis", "build_deep_report", "_sanitize_deep_reason"]

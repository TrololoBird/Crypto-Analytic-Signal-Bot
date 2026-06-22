"""Telegram HTML rendering for the deep-analysis report."""
from __future__ import annotations

from hunt_core.scanner.detect.deep.forecast import Scenario
from hunt_core.scanner.detect.deep.panel import DeepPanel
from hunt_core.scanner.detect.deep.verdict import Verdict

_SIDE_EMOJI = {"long": "🟢 LONG lean", "short": "🔴 SHORT lean", "none": "⚪ no lean"}


def _fmt_price(p: float | None) -> str:
    if p is None:
        return "—"
    ap = abs(p)
    if ap >= 1000:
        return f"{p:,.2f}"
    if ap >= 1:
        return f"{p:.4f}"
    if ap >= 0.01:
        return f"{p:.6f}"
    return f"{p:.8f}"


def format_deep_telegram(panel: DeepPanel, scenarios: list[Scenario], verdict: Verdict) -> str:
    lines: list[str] = []
    lines.append(f"🔎 <b>Deep analysis — {panel.symbol}</b> <code>{panel.tf}</code>")
    lines.append(f"price <code>{_fmt_price(panel.price)}</code> · phase <b>{panel.phase.phase}</b>")
    lines.append("")

    # Lean + confidence
    side = _SIDE_EMOJI.get(panel.side, panel.side)
    lines.append(
        f"{side} · confidence <b>{panel.confidence:.0%}</b> "
        f"(z={panel.fusion.z_dir:+.2f}, {panel.fusion.n_active} factors"
        f"{'' if panel.fusion.agreement else ', ⚠ disagree'})"
    )
    lines.append("")

    # Verdict
    flag = "✅" if verdict.actionable else "⚠️"
    lines.append(f"{flag} <b>{verdict.headline}</b>")
    lines.append(f"<i>{verdict.rationale}</i>")
    lines.append("")

    # Factor breakdown
    if panel.readings:
        lines.append("<b>Factors</b>")
        for r in panel.readings:
            sign = "" if r.kind == "amplifier" else (f" ({r.score:+.2f})")
            lines.append(f"• {r.text}{sign}")
        lines.append("")

    # Forecast scenarios
    if scenarios:
        lines.append("<b>ATR projection</b> (±1σ random-walk)")
        for s in scenarios:
            lines.append(
                f"• {s.label}: base <code>{_fmt_price(s.base)}</code> "
                f"({s.drift_pct:+.2f}%) · band <code>{_fmt_price(s.low)}</code>…"
                f"<code>{_fmt_price(s.high)}</code>"
            )
        lines.append("")

    lines.append("<i>Statistical read, not a signal — manual review required.</i>")
    return "\n".join(lines)


__all__ = ["format_deep_telegram"]

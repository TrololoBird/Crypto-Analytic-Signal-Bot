"""Deep analysis orchestrator."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hunt_core.deep.forecast_panel import build_structural_forecast_panel
from hunt_core.deep.verdicts import build_three_verdicts
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
        _TREND_RU = {"bull": "вверх", "bear": "вниз", "neutral": "нейтр", "long": "вверх", "short": "вниз"}
        _SIDE_RU = {"long": "лонг", "short": "шорт", "neutral": "боковик", "sideways": "боковик"}
        lines = ["📐 <b>МТФ структура</b>"]
        tf_signals = getattr(mtf, "tf_signals", None) or {}
        for tf_key in ("1w", "1d", "4h", "1h", "15m", "5m"):
            sig = tf_signals.get(tf_key)
            if sig is None:
                continue
            trend = getattr(sig, "trend", None) or (
                sig.get("trend") if isinstance(sig, dict) else None
            )
            if trend:
                trend_ru = _TREND_RU.get(str(trend), str(trend))
                lines.append(f"  {tf_key}: <b>{html.escape(trend_ru)}</b>")
        long_s = getattr(mtf, "long_scenario", None)
        short_s = getattr(mtf, "short_scenario", None)
        if long_s and short_s:
            lines.append(
                f"  лонг {float(getattr(long_s, 'score', 0)):.2f} "
                f"({getattr(long_s, 'htf_count', 0)}/{getattr(long_s, 'htf_total', 0)} HTF) · "
                f"шорт {float(getattr(short_s, 'score', 0)):.2f} "
                f"({getattr(short_s, 'htf_count', 0)}/{getattr(short_s, 'htf_total', 0)} HTF)"
            )
        dom = getattr(mtf, "dominant", None)
        if dom:
            dom_ru = _SIDE_RU.get(str(dom), str(dom))
            lines.append(f"  преобладает: <b>{html.escape(dom_ru)}</b>")
        return "\n".join(lines) if len(lines) > 1 else ""

    def verdicts_text(self) -> str:
        v = self.verdicts
        _SIDE_RU = {"long": "лонг", "short": "шорт", "sideways": "боковик", "neutral": "нет"}
        lines = ["📊 <b>Вердикты</b> (структура-сначала)"]
        for key in ("long", "short", "sideways"):
            block = v.get(key) or {}
            score = block.get("score", 0)
            conf = block.get("confidence", 0)
            key_ru = _SIDE_RU.get(key, key)
            lines.append(f"  {key_ru}: оценка <code>{score:.2f}</code> увер. <code>{conf:.0%}</code>")
        dom = v.get("dominant") or "neutral"
        dom_ru = _SIDE_RU.get(str(dom), str(dom))
        lines.append(f"Преобладает: <b>{dom_ru}</b>")
        reason = v.get("reason")
        if reason:
            clean = _sanitize_deep_reason(str(reason))
            if clean:
                _GATE_RU = {
                    "timing_c": "ждём подтверждения",
                    "timing_a": "рано — ждём",
                    "timing_b": "нет триггера",
                    "rr_primary": "R:R недостаточный",
                    "conviction": "низкая убеждённость",
                    "structure": "структура не подтверждена",
                    "confluence": "нет слияния факторов",
                    "strength": "сила сигнала низкая",
                    "fragility": "высокая хрупкость",
                }
                clean = clean.replace("WAIT:", "ЖДЁМ:")
                for code, ru in _GATE_RU.items():
                    clean = clean.replace(code, ru)
                lines.append(f"<i>{html.escape(clean)}</i>")
        src = v.get("source")
        if src:
            lines.append(f"<i>источник: {src}</i>")
        return "\n".join(lines)

    def indicator_panel_text(self) -> str:
        return ""

    def verdict_v2_text(self) -> str:
        from hunt_core.deep.verdict_v2.types import ScenarioVerdict

        v2 = self.row.get("verdict_v2")
        summary = self.row.get("verdict_v2_summary")
        # After a store/JSONL round-trip ``verdict_v2`` survives only as a plain
        # dict (the dataclass is stripped on encode). The rich renderer needs the
        # ScenarioVerdict object, so anything that is NOT one falls back to the
        # JSONL-safe summary block instead of crashing on ``v2.signal_decision``.
        if not isinstance(v2, ScenarioVerdict):
            if isinstance(summary, dict) and summary:
                from hunt_core.deep.format_pinned_signal import (
                    _ACTION_RU,
                    _ru_narrative,
                )

                action_raw = str(summary.get("action") or "wait").upper()
                action_ru = _ACTION_RU.get(action_raw, action_raw)
                emoji = {"LONG": "🟢", "SHORT": "🔴", "WAIT": "⏳"}.get(action_raw, "⏳")
                path = _ru_narrative(str(summary.get("path", "")))
                strength = summary.get("strength", 0)
                lines = [
                    f"{emoji} <b>Сигнал</b> · <b>{action_ru}</b> · "
                    f"сценарий <code>{html.escape(path)}</code> · "
                    f"сила <code>{float(strength):.2f}</code>",
                ]
                if summary.get("gates_failed"):
                    gates = ", ".join(str(g) for g in summary["gates_failed"])
                    lines.append(f"<i>ожидаем: {html.escape(gates)}</i>")
                lines.append("<i>ранг, не вероятность</i>")
                return "\n".join(lines)
            return ""
        from hunt_core.deep.format_pinned_signal import format_pinned_signal

        block = format_pinned_signal(self.row, verdict=v2)
        return block if block else ""


def _enrich_deep_row(work: dict[str, Any]) -> dict[str, Any]:
    sym = str(work.get("symbol") or "").upper()
    tf = work.get("timeframes") or {}
    price = float(work.get("price") or 0)
    if not sym or not tf or price <= 0:
        return work

    from hunt_core.deep.pinned import build_pinned_verdict

    if not work.get("pinned_verdict"):
        work["pinned_verdict"] = build_pinned_verdict(work)
    from hunt_core.deep.verdict_v2.serialize import attach_verdict_v2_to_row

    attach_verdict_v2_to_row(work)
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

    from hunt_core.deep.structural_forecast import (
        build_structural_down_forecast,
        build_structural_up_forecast,
    )

    up_fc = build_structural_up_forecast(work)
    down_fc = build_structural_down_forecast(work)
    forecasts = {
        "structural_up": up_fc,
        "structural_down": down_fc,
    }
    fusion = work.get("manipulation_fusion") if isinstance(work.get("manipulation_fusion"), dict) else {}

    verdicts = build_three_verdicts(work, fusion=fusion)
    work["_deep_verdicts"] = verdicts
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

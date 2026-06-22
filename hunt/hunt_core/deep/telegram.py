"""Module 1 Deep RU Telegram formatter (canonical macquette)."""
from __future__ import annotations

from typing import Any

from hunt_core.deliver.telegram import fmt_price


def format_deep_signal(
    *,
    symbol: str,
    side: str,
    scenario: str,
    horizon: str,
    reliability: str,
    entry_lo: float,
    entry_hi: float,
    activation: str,
    stop: float,
    tp1: float,
    tp2: float,
    sl_pct: float,
    tp1_pct: float,
    tp2_pct: float,
    invalidation: str,
    context_lines: list[str],
) -> str:
    icon = "🟢" if side.upper() == "LONG" else "🔴" if side.upper() == "SHORT" else "⏳"
    verdict = side.upper() if side else "WAIT"
    ctx = "\n".join(context_lines) if context_lines else ""
    return (
        f"{icon} {symbol} · Глубокий анализ\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Вывод: {verdict} (ждать триггер)\n"
        f"Сценарий: {scenario}\n"
        f"Горизонт: {horizon} · Надёжность сценария: {reliability}\n\n"
        f"📋 План сделки\n"
        f"Зона входа: {fmt_price(entry_lo)}–{fmt_price(entry_hi)}\n"
        f"Активация: {activation}\n"
        f"Stop-loss: {fmt_price(stop)} ({sl_pct:+.1f}%)\n"
        f"Цель 1: {fmt_price(tp1)} ({tp1_pct:+.1f}%) · Цель 2: {fmt_price(tp2)} ({tp2_pct:+.1f}%)\n"
        f"Инвалидация: {invalidation}\n\n"
        f"🔎 Контекст\n"
        f"{ctx}\n\n"
        f"⚠️ Аналитический сигнал, не инвестиционная рекомендация. Вход — вручную."
    )


def format_deep_from_verdict(symbol: str, verdict: dict[str, Any]) -> str | None:
    plan = verdict.get("trade_plan") or {}
    if not plan:
        return None
    side = str(verdict.get("direction") or plan.get("side") or "WAIT")
    entry = plan.get("entry_zone") or plan.get("entry_band") or [0, 0]
    lo, hi = float(entry[0]), float(entry[1])
    stop = float(plan.get("stop") or plan.get("stop_loss") or 0)
    tp1 = float(plan.get("tp1") or plan.get("targets", [0])[0] if plan.get("targets") else 0)
    tp2 = float(plan.get("tp2") or (plan.get("targets") or [0, 0])[1] if len(plan.get("targets") or []) > 1 else tp1)
    mid = (lo + hi) / 2 if lo and hi else lo or hi or 1.0
    sl_pct = (stop - mid) / mid * 100 if mid else 0
    tp1_pct = (tp1 - mid) / mid * 100 if mid else 0
    tp2_pct = (tp2 - mid) / mid * 100 if mid else 0
    return format_deep_signal(
        symbol=symbol,
        side=side,
        scenario=str(verdict.get("expected_path") or verdict.get("scenario") or "—"),
        horizon=str(verdict.get("horizon") or "1–2 дня"),
        reliability=str(verdict.get("strength_label") or "средняя"),
        entry_lo=lo,
        entry_hi=hi,
        activation=str(plan.get("activation") or "лимит в зоне"),
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        sl_pct=sl_pct,
        tp1_pct=tp1_pct,
        tp2_pct=tp2_pct,
        invalidation=str(plan.get("invalidation") or "—"),
        context_lines=list(verdict.get("context_lines") or []),
    )


__all__ = ["format_deep_from_verdict", "format_deep_signal"]

"""Module 2 Scanner RU Telegram formatter (canonical macquette)."""
from __future__ import annotations

from typing import Any

from hunt_core.deliver.telegram import fmt_price


def format_scanner_signal(
    *,
    symbol: str,
    archetype: str,
    state_desc: str,
    strength: float,
    side: str,
    entry_lo: float,
    entry_hi: float,
    trigger: str,
    stop: float,
    tp1: float,
    tp2: float,
    sl_pct: float,
    tp1_pct: float,
    tp2_pct: float,
    invalidation: str,
    context_lines: list[str],
    ttl_hours: float,
    lab: bool = False,
) -> str:
    icon = "🔴" if side.upper() == "SHORT" else "🟢"
    prefix = "🧪 ЛАБ · " if lab else ""
    ctx = "\n".join(context_lines) if context_lines else ""
    return (
        f"{prefix}{icon} {symbol} · {archetype}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Состояние: {state_desc}\n"
        f"Сила сигнала: {strength:.0f} / 100 (это сила, не вероятность)\n\n"
        f"📋 Сигнал · {side.upper()}\n"
        f"Зона входа: {fmt_price(entry_lo)}–{fmt_price(entry_hi)} (лимит)\n"
        f"Триггер активации: {trigger}\n"
        f"Stop-loss: {fmt_price(stop)} ({sl_pct:+.1f}%)\n"
        f"Цель 1: {fmt_price(tp1)} ({tp1_pct:+.1f}%) · Цель 2: {fmt_price(tp2)} ({tp2_pct:+.1f}%)\n"
        f"Инвалидация: {invalidation}\n\n"
        f"🔎 Сопровождение\n"
        f"{ctx}\n"
        f"Срок актуальности: ~{ttl_hours:.0f} ч\n\n"
        f"⚠️ Аналитический сигнал, не инвестиционная рекомендация. Вход — вручную."
    )


def format_scanner_coil_bracket(
    *,
    symbol: str,
    strength: float,
    price: float,
    break_long: float,
    break_short: float,
    state_desc: str,
    context_lines: list[str],
    ttl_hours: float = 2.0,
    lab: bool = False,
) -> str:
    """High energy + undecided direction — bracket ARMED signal (plan P0-B)."""
    prefix = "🧪 ЛАБ · " if lab else ""
    ctx = "\n".join(context_lines) if context_lines else ""
    return (
        f"{prefix}⏳ {symbol} · Сжатие (сканер)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Состояние: {state_desc}\n"
        f"Сила сигнала: {strength:.0f} / 100 (это сила, не вероятность)\n\n"
        f"📋 Условный сигнал · ARMED\n"
        f"Сжатие на {fmt_price(price)} — направление не определено\n"
        f"Пробой выше {fmt_price(break_long)} → LONG\n"
        f"Пробой ниже {fmt_price(break_short)} → SHORT\n\n"
        f"🔎 Сопровождение\n"
        f"{ctx}\n"
        f"Срок актуальности: ~{ttl_hours:.0f} ч\n\n"
        f"⚠️ Аналитический сигнал, не инвестиционная рекомендация. Вход — вручную."
    )


def format_scanner_from_setup(symbol: str, setup: dict[str, Any], row: dict[str, Any], *, lab: bool = False) -> str | None:
    if not setup.get("confirmed"):
        return None
    phase = str(setup.get("phase") or row.get("lifecycle", {}).get("phase") or "")
    reasons = setup.get("reasons") or []
    direction = str(setup.get("direction") or setup.get("side") or "short")
    is_coil = phase == "coil" or "coil_bracket_armed" in reasons
    price = float(row.get("price") or 0)
    m = row.get("market") if isinstance(row.get("market"), dict) else {}
    ctx: list[str] = []
    if m.get("funding_rate") is not None:
        ctx.append(f"Фандинг: {float(m['funding_rate']):.4f}")
    if m.get("oi_slope_5m") is not None:
        ctx.append(f"OI slope 5m: {float(m['oi_slope_5m']):+.3f}")
    dr = row.get("data_readiness")
    if isinstance(dr, dict) and dr.get("composite_pct") is not None:
        ctx.append(f"Готовность данных: {int(dr['composite_pct'])} / 100")
    strength = float(setup.get("fusion_score") or setup.get("dump_score") or setup.get("long_score") or 0)
    if is_coil and (direction in {"none", ""} or direction.lower() == "undecided") and price > 0:
        atr = float(m.get("atr14") or m.get("atr") or price * 0.01)
        break_long = price + atr * 0.5
        break_short = price - atr * 0.5
        return format_scanner_coil_bracket(
            symbol=symbol,
            strength=strength,
            price=price,
            break_long=break_long,
            break_short=break_short,
            state_desc="сжатие волатильности, направление открыто",
            context_lines=ctx,
            ttl_hours=float(setup.get("ttl_minutes") or 120) / 60.0,
            lab=lab,
        )
    ez = setup.get("entry_zone") or [row.get("price"), row.get("price")]
    lo, hi = float(ez[0]), float(ez[1])
    stop = float(setup.get("stop_loss") or 0)
    tp1 = float(setup.get("tp1") or 0)
    tp2 = float(setup.get("tp2") or tp1)
    mid = (lo + hi) / 2 if lo and hi else float(row.get("price") or 1)
    sl_pct = (stop - mid) / mid * 100 if direction == "short" else (mid - stop) / mid * 100
    tp1_pct = (mid - tp1) / mid * 100 if direction == "short" else (tp1 - mid) / mid * 100
    tp2_pct = (mid - tp2) / mid * 100 if direction == "short" else (tp2 - mid) / mid * 100
    mf = row.get("manipulation_fusion") or {}
    arch = str(mf.get("archetype") or "Сканер")
    return format_scanner_signal(
        symbol=symbol,
        archetype=arch,
        state_desc=str(setup.get("phase") or row.get("lifecycle", {}).get("phase") or "—"),
        strength=float(setup.get("fusion_score") or setup.get("dump_score") or setup.get("long_score") or 0),
        side=direction,
        entry_lo=lo,
        entry_hi=hi,
        trigger=str(setup.get("trigger") or "закрытие 5m"),
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        sl_pct=sl_pct,
        tp1_pct=tp1_pct,
        tp2_pct=tp2_pct,
        invalidation=str(setup.get("invalidation") or "—"),
        context_lines=ctx,
        ttl_hours=float(setup.get("ttl_minutes") or 120) / 60.0,
        lab=lab,
    )


__all__ = ["format_scanner_coil_bracket", "format_scanner_from_setup", "format_scanner_signal"]

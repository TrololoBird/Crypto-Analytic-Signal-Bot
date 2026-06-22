"""Table-driven Telegram templates (§K / §8D)."""
from __future__ import annotations

import html
from typing import Any


def format_confirm_strong(
    row: dict[str, Any],
    *,
    direction: str,
    delivery_tier: str = "triggered",
    confirm_reasons: list[str] | None = None,
) -> str:
    """Confirm TG body — delegates to unified delivery card formatter."""
    from hunt_core.deliver.dispatch import format_delivery_card

    setup = row.get("dump") if direction == "short" else row.get("long")
    setup = setup if isinstance(setup, dict) else {}
    return format_delivery_card(
        row,
        direction=direction,
        setup=setup,
        delivery_tier=delivery_tier,
        confirm_reasons=confirm_reasons,
    )


def format_advisory_early(row: dict[str, Any], *, note: str) -> str:
    sym = str(row.get("symbol") or "").replace("USDT", "-USDT")
    return f"⏳ <b>{sym}</b> · EARLY advisory\n{note}"


def format_pinned_summary(row: dict[str, Any]) -> str:
    sym = str(row.get("symbol") or "").replace("USDT", "-USDT")
    verdict = row.get("pinned_verdict") or row.get("pinned_scenario") or {}
    direction = verdict.get("direction") or verdict.get("primary_direction") or "—"
    return f"📌 <b>{sym}</b> · {direction}"


def format_telegram_confirm(
    row: dict[str, Any],
    *,
    direction: str,
    confirm_reasons: list[str],
    delivery_tier: str = "triggered",
) -> str:
    """Closed-bar confirm card + optional confluence grid."""
    from hunt_core.analysis.confluence_grid import build_confluence_grid, format_grid_telegram

    setup = row["dump"] if direction == "short" else row["long"]
    body = format_confirm_strong(
        {**row, "dump" if direction == "short" else "long": setup},
        direction=direction,
        delivery_tier=delivery_tier,
        confirm_reasons=confirm_reasons,
    )
    if "Карта уровней" not in body:
        grid = build_confluence_grid(row)
        if grid:
            body = f"{body}\n{format_grid_telegram(grid)}"
    return body


def format_squeeze_telegram(row: dict[str, Any]) -> str:
    """Squeeze advisory card — canonical template (was deliver/telegram.py)."""
    from hunt_core.deliver._labels import fmt_price, phase_human

    sym = html.escape(str(row["symbol"]).replace("USDT", "-USDT"))
    sq = row.get("squeeze") or {}
    vol = row.get("vol_24h_m")
    vol_str = f"{vol:.0f}M" if vol is not None else "—"

    don = sq.get("donchian_width_pct_1h")
    compression_str = f"{don:.1f}%" if don is not None else "—"

    dir_emoji, dir_label, evidence = _squeeze_direction(row, phase_human=phase_human)
    evidence_txt = "\n".join(f"   · {e}" for e in evidence) if evidence else "   · нет сигналов"

    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    res = dump.get("resistance_liq") or dump.get("support_break_level")
    sup = long_setup.get("support_zone") or dump.get("support_break_level")
    level_parts: list[str] = []
    if res:
        level_parts.append(f"Сопротивление <code>{fmt_price(float(res))}</code>")
    if sup and sup != res:
        level_parts.append(f"Поддержка <code>{fmt_price(float(sup))}</code>")
    levels_line = "  |  ".join(level_parts) if level_parts else ""

    lines = [
        f"⚡ <b>СЖАТИЕ ЗАРЯЖЕНО · {sym}</b>",
        (
            f"Волатильность сжата до {compression_str} от диапазона — "
            f"ожидается сильный пробой. Объём 24h: <code>{vol_str}</code>"
        ),
        "",
        f"{dir_emoji} <b>Вероятное направление: {dir_label}</b>",
        evidence_txt,
    ]
    if levels_line:
        lines += ["", f"📍 {levels_line}"]
    lines += ["", "<i>Watch-only — вход только по confirmed-сигналу системы.</i>"]
    return "\n".join(lines)


def _squeeze_direction(
    row: dict[str, Any],
    *,
    phase_human: Any,
) -> tuple[str, str, list[str]]:
    """Infer probable breakout direction. Returns (emoji, label, evidence_lines)."""
    sq = row.get("squeeze") or {}
    lifecycle = row.get("lifecycle") or {}
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}

    bear = 0
    bull = 0
    evidence: list[str] = []

    bias = str(lifecycle.get("recommended_bias") or "")
    lc_phase = str(lifecycle.get("phase") or "")
    phase_txt = phase_human(lc_phase) if lc_phase else ""
    if bias == "short":
        bear += 2
        evidence.append(f"Lifecycle: {html.escape(phase_txt)} (медвежий)")
    elif bias == "long":
        bull += 2
        evidence.append(f"Lifecycle: {html.escape(phase_txt)} (бычий)")
    elif phase_txt:
        evidence.append(f"Lifecycle: {html.escape(phase_txt)}")

    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    struct_bias = str(
        structure.get("structure_bias")
        or lifecycle.get("structure_bias")
        or ""
    ).lower()
    if struct_bias == "short":
        bear += 2
        evidence.append("Structure: медвежий BOS/CHoCH")
    elif struct_bias == "long":
        bull += 2
        evidence.append("Structure: бычий BOS/CHoCH")

    from hunt_core.scanner.detect.setup_fields import setup_conviction_pct, setup_p_win

    def _side_strength(setup: dict[str, Any], *, direction: str) -> float:
        p = setup_p_win(setup)
        if p is not None:
            return p * 100.0
        return setup_conviction_pct(setup, direction=direction)

    short_strength = _side_strength(dump, direction="short")
    long_strength = _side_strength(long_setup, direction="long")
    if short_strength > long_strength + 10:
        bear += 1
        evidence.append(
            f"Conviction шорт {short_strength:.0f} > лонг {long_strength:.0f}"
        )
    elif long_strength > short_strength + 10:
        bull += 1
        evidence.append(
            f"Conviction лонг {long_strength:.0f} > шорт {short_strength:.0f}"
        )

    try:
        oi_z = float(sq.get("oi_z") or 0)
        if oi_z < -1.2:
            bear += 1
            evidence.append(f"OI падает ({oi_z:+.2f}σ) — позиции сокращаются")
        elif oi_z > 1.2:
            bull += 1
            evidence.append(f"OI растёт ({oi_z:+.2f}σ) — накопление")
        elif abs(oi_z) > 0.3:
            evidence.append(f"OI z={oi_z:+.2f}σ (нейтрально)")
    except (TypeError, ValueError):
        pass

    # Normalize funding to a decimal fraction (0.0001 ≈ 0.01%). funding_pct is stored
    # as a percent number (funding_rate*100), so it must be /100 — mixing it with the
    # raw funding_rate fraction under one threshold mis-scaled both the thresholds and
    # the displayed % by 100x (M10).
    try:
        from hunt_core.contract import normalize_funding_fraction

        fund = normalize_funding_fraction(sq.get("funding_rate"))
        if fund is None:
            fund = normalize_funding_fraction(sq.get("funding_pct"))
        if fund is None:
            fund = 0.0
        if fund >= 0.001:
            bear += 1
            evidence.append(f"Funding перегрет ({fund * 100:.3f}%) — лонги платят")
        elif fund <= -0.0001:
            bull += 1
            evidence.append(f"Funding отрицательный ({fund * 100:.3f}%) — шорты платят")
        elif abs(fund) > 1e-6:
            evidence.append(f"Funding {fund * 100:.3f}% (нейтрально)")
    except (TypeError, ValueError):
        pass

    if bear > bull:
        return "🔴", "ВНИЗ — вероятен шорт-пробой", evidence
    if bull > bear:
        return "🟢", "ВВЕРХ — вероятен лонг-пробой", evidence
    if short_strength > long_strength:
        return "🔴", "СЛАБЫЙ УКЛОН ВНИЗ (conviction short>long)", evidence
    if long_strength > short_strength:
        return "🟢", "СЛАБЫЙ УКЛОН ВВЕРХ (conviction long>short)", evidence
    return "⚪", "НЕЙТРАЛЬНО — ждать closed-bar confirm", evidence


def format_followup_telegram_message(followup: Any, row: dict[str, Any]) -> str:
    from hunt_core.deliver.telegram import format_followup_telegram as _fmt

    return _fmt(followup, row)


def format_setup_lines_for_probe(
    row: dict[str, Any],
    setup: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any],
    pos: dict[str, Any],
    price: float,
) -> list[str]:
    """Re-export for /signal probe — canonical body in deliver.telegram."""
    from hunt_core.deliver.telegram import format_setup_lines as _fmt

    return _fmt(row, setup, direction=direction, tf=tf, pos=pos, price=price)


__all__ = [
    "format_advisory_early",
    "format_confirm_strong",
    "format_followup_telegram_message",
    "format_pinned_summary",
    "format_setup_lines_for_probe",
    "format_squeeze_telegram",
    "format_telegram_confirm",
]

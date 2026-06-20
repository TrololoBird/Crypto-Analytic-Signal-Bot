"""Telegram /signal brief + scenario lines (pinned + probe)."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.deliver._labels import fmt_price, format_symbol_telegram, phase_human


_DUMP_LIFECYCLE_PHASES = frozenset(
    {
        "dump_initiating",
        "dump_active",
        "exhaustion_at_high",
        "post_dump_bounce",
        "distribution",
    }
)
_PUMP_LIFECYCLE_PHASES = frozenset(
    {
        "impulse_initiating",
        "impulse_active",
        "accumulation",
        "markup",
    }
)


def _pct_str(a: float, b: float, direction: str) -> str:
    from hunt_core.deliver.dispatch import _pct_str as _p
    return _p(a, b, direction)


def _risk_pct_str(entry: float, stop: float | None, direction: str) -> str:
    from hunt_core.deliver.dispatch import _risk_pct_str as _r
    return _r(entry, stop, direction)


def _worst_entry_edge(entry_lo: float, entry_hi: float, *, direction: str, price: float) -> float:
    from hunt_core.deliver.dispatch import _worst_entry_from_setup
    return _worst_entry_from_setup({"entry_zone": [entry_lo, entry_hi]}, direction=direction, price=price)


_fmt_price = fmt_price


def _zone_status(
    *,
    direction: str,
    price: float,
    entry_lo: float,
    entry_hi: float,
) -> str:
    if entry_lo <= 0 or entry_hi <= 0 or price <= 0:
        return ""
    if entry_lo <= price <= entry_hi:
        return "в зоне"
    if direction == "short":
        if price < entry_lo:
            return "ждём откат ↑"
        return "выше зоны"
    if price > entry_hi:
        return "ждём откат ↓"
    return "ниже зоны"


def _hunt_scenario_lines(
    setup: dict[str, Any],
    *,
    direction: str,
    row: dict[str, Any],
    price: float,
    active: bool = False,
    mtf_sc: Any | None = None,
) -> list[str]:
    """User-facing scenario from hunt detector levels + readiness (not raw MTF score)."""
    from hunt_core.deliver.dispatch import display_readiness_score, geometry_block_evidence

    emoji = "📈" if direction == "long" else "📉"
    label = "ЛОНГ" if direction == "long" else "ШОРТ"
    readiness = display_readiness_score(setup, direction=direction, row=row)
    geo = geometry_block_evidence(setup, row=row, direction=direction)
    phase = str(setup.get("phase") or "—")
    confirmed = bool(setup.get("confirmed"))
    star = " ★" if active or confirmed else ""

    ez = setup.get("entry_zone") or []
    try:
        entry_lo = float(ez[0]) if len(ez) >= 1 else 0.0
        entry_hi = float(ez[1]) if len(ez) >= 2 else entry_lo
    except (TypeError, ValueError):
        entry_lo = entry_hi = 0.0

    htf = ""
    if mtf_sc is not None and int(getattr(mtf_sc, "htf_total", 0) or 0) > 0:
        hc = int(getattr(mtf_sc, "htf_count", 0))
        ht = int(getattr(mtf_sc, "htf_total", 0))
        if hc > 0 or not confirmed:
            htf = f" · HTF {hc}/{ht}"

    if entry_lo <= 0 or entry_hi <= 0:
        return [f"{emoji} <b>{label}</b> · нет валидных уровней"]

    sl = setup.get("stop_loss")
    tp1 = setup.get("tp1")
    rr = setup.get("risk_reward")
    edge = _worst_entry_edge(entry_lo, entry_hi, direction=direction, price=price)
    tp1_pct = _pct_str(edge, float(tp1), direction) if tp1 else ""
    sl_pct = _risk_pct_str(edge, float(sl) if sl is not None else None, direction) if sl else ""
    zone = _zone_status(
        direction=direction, price=price, entry_lo=entry_lo, entry_hi=entry_hi
    )
    zone_txt = f" · <i>{zone}</i>" if zone else ""

    lines = [
        (
            f"{emoji} <b>{label}</b>{star} · готовность <code>{readiness:.0f}</code>"
            f"{htf} · <code>{html.escape(phase)}</code>"
        ),
        (
            f"Вход <code>{_fmt_price(entry_lo)}–{_fmt_price(entry_hi)}</code>{zone_txt}"
            f" → TP <code>{_fmt_price(tp1)}</code>"
            f"{f' ({tp1_pct})' if tp1_pct else ''}"
            f" · SL <code>{_fmt_price(sl)}</code>"
            f"{f' ({sl_pct})' if sl_pct else ''}"
            + (
                f" · R:R (худший) <code>{float(rr):.2f}</code>"
                if rr is not None
                else ""
            )
        ),
    ]
    geo_reason = geo.get("reason")
    if geo_reason:
        lines.append(f"⚠️ <i>{html.escape(str(geo_reason))}</i>")
    elif setup.get("levels_viable") is False:
        lines.append("⚠️ <i>уровни не прошли валидацию</i>")
    return lines


def _compact_scenario_lines(sc: Any, *, emoji: str, label: str, active: bool = False) -> list[str]:
    """3-line scenario block for user-facing /signal."""
    if sc is None:
        return [f"{emoji} <b>{label}</b> · нет данных"]
    entry_lo = float(getattr(sc, "entry_lo", 0) or 0)
    entry_hi = float(getattr(sc, "entry_hi", 0) or 0)
    tp1 = float(getattr(sc, "tp1", 0) or 0)
    stop = float(getattr(sc, "stop", 0) or 0)
    score = float(getattr(sc, "score", 0) or 0)
    htf_count = int(getattr(sc, "htf_count", 0) or 0)
    htf_total = int(getattr(sc, "htf_total", 0) or 0)
    evidence = list(getattr(sc, "evidence", []) or [])
    star = " ★" if active else ""
    htf = f" · HTF {htf_count}/{htf_total}" if htf_total else ""
    out = [
        f"{emoji} <b>{label}</b>{star} · score <code>{score:.2f}</code>{htf}",
        (
            f"Вход <code>{_fmt_price(entry_lo)}–{_fmt_price(entry_hi)}</code>"
            f" → TP <code>{_fmt_price(tp1)}</code>"
            f" · SL <code>{_fmt_price(stop)}</code>"
        ),
    ]
    if evidence:
        note = str(evidence[0])
        if len(note) > 72:
            note = note[:69] + "…"
        out.append(f"<i>{html.escape(note)}</i>")
    return out


def _setup_scenario_lines(
    setup: dict[str, Any],
    *,
    direction: str,
    price: float,
    active: bool = False,
) -> list[str]:
    """Fallback scenario when MTF object is unavailable."""
    emoji = "📈" if direction == "long" else "📉"
    label = "ЛОНГ" if direction == "long" else "ШОРТ"
    fuel = float(setup.get("long_fuel" if direction == "long" else "dump_fuel") or 0)
    phase = str(setup.get("phase") or "—")
    ez = setup.get("entry_zone") or [price, price]
    star = " ★" if active or setup.get("confirmed") else ""
    return [
        f"{emoji} <b>{label}</b>{star} · fuel <code>{fuel:.0f}</code> · <code>{html.escape(phase)}</code>",
        (
            f"Вход <code>{_fmt_price(float(ez[0]))}–{_fmt_price(float(ez[1] if len(ez) > 1 else ez[0]))}</code>"
            f" → TP <code>{_fmt_price(setup.get('tp1'))}</code>"
            f" · SL <code>{_fmt_price(setup.get('stop_loss'))}</code>"
        ),
    ]


def _brief_reason(row: dict[str, Any]) -> str:
    lc = row.get("lifecycle") or {}
    lc_phase = str(lc.get("phase") or "")
    if lc_phase == "no_setup":
        return "Нет structural setup — оба сценария справочные, вход только по confirm"

    verdict = row.get("pinned_verdict")
    if verdict is not None:
        reason = str(getattr(verdict, "reason", "") or "")
    else:
        reason = (
            f"phase={lc.get('phase') or '—'}"
            f" · bias={lc.get('recommended_bias') or '—'}"
        )
    parts = [p.strip() for p in reason.replace(";", "·").split("·") if p.strip()]
    short = " · ".join(parts[:2]) if parts else "ждём closed-bar confirm"
    return short[:160] + ("…" if len(short) > 160 else "")


def _entry_zones_overlap(
    setup_a: dict[str, Any],
    setup_b: dict[str, Any],
    *,
    tol_pct: float = 0.2,
) -> bool:
    """True when two setups share essentially the same entry band (EPIC dual-card bug)."""
    try:
        a = setup_a.get("entry_zone") or []
        b = setup_b.get("entry_zone") or []
        if len(a) < 2 or len(b) < 2:
            return False
        a_lo, a_hi = float(a[0]), float(a[1])
        b_lo, b_hi = float(b[0]), float(b[1])
        mid = (a_lo + a_hi) / 2.0
        if mid <= 0:
            return False
        return (
            abs(a_lo - b_lo) / mid * 100.0 <= tol_pct
            and abs(a_hi - b_hi) / mid * 100.0 <= tol_pct
        )
    except (TypeError, ValueError):
        return False


def _alt_scenario_one_liner(
    setup: dict[str, Any],
    *,
    direction: str,
    row: dict[str, Any],
) -> str:
    from hunt_core.deliver.dispatch import display_readiness_score, geometry_block_reason

    label = "Лонг" if direction == "long" else "Шорт"
    readiness = display_readiness_score(setup, direction=direction, row=row)
    geo = geometry_block_reason(setup, row=row, direction=direction)
    if geo:
        return f"↔ <i>{label}: контр-сценарий · {html.escape(str(geo))}</i>"
    if setup.get("levels_viable") is False:
        return f"↔ <i>{label}: контр-сценарий · уровни не для входа</i>"
    return (
        f"↔ <i>{label}: контр-сценарий · готовность {readiness:.0f}/100 "
        f"(не вход до confirm)</i>"
    )


def format_signal_brief_telegram(
    row: dict[str, Any],
    *,
    confirmed_direction: str | None = None,
    would_deliver: bool = False,
    added_watch: bool = False,
    delivery_tier: Any = None,
) -> str:
    """User /signal reply: primary scenario + optional collapsed alternate."""
    from hunt_core.detect.probe import probe_header, resolve_trade_direction

    sym = format_symbol_telegram(str(row.get("symbol", "?")))
    price = float(row.get("price") or 0)
    lc = row.get("lifecycle") or {}
    lc_phase_raw = str(lc.get("phase") or "—")
    phase = html.escape(phase_human(lc_phase_raw))
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    mtf = row.get("mtf")
    long_mtf = getattr(mtf, "long_scenario", None) if mtf is not None else None
    short_mtf = getattr(mtf, "short_scenario", None) if mtf is not None else None

    primary_dir, _, _, _ = resolve_trade_direction(row)
    if confirmed_direction:
        primary_dir = confirmed_direction
    alt_dir = "short" if primary_dir == "long" else "long"
    primary_setup = long_setup if primary_dir == "long" else dump
    alt_setup = dump if alt_dir == "short" else long_setup
    primary_mtf = long_mtf if primary_dir == "long" else short_mtf
    alt_mtf = short_mtf if alt_dir == "short" else long_mtf

    lifecycle_note = ""
    if primary_dir == "long" and lc_phase_raw in _DUMP_LIFECYCLE_PHASES:
        lifecycle_note = " · <i>⚠️ lifecycle дамп — лонг контр-тренд</i>"
    elif primary_dir == "short" and lc_phase_raw in _PUMP_LIFECYCLE_PHASES:
        lifecycle_note = " · <i>⚠️ lifecycle pump — шорт контр-тренд</i>"

    lines: list[str] = [
        f"🔭 <b>{sym}</b> · <code>{_fmt_price(price)}</code> · {phase}{lifecycle_note}",
    ]
    if confirmed_direction:
        dir_ru = "ШОРТ" if confirmed_direction == "short" else "ЛОНГ"
        if would_deliver:
            lines.append(f"✅ <b>Delivery {dir_ru}</b> — алерт прошёл бы сейчас")
            if delivery_tier is not None:
                tier = getattr(delivery_tier, "tier", None) or (
                    delivery_tier.get("tier") if isinstance(delivery_tier, dict) else None
                )
                if tier:
                    lines.append(f"Tier: <code>{html.escape(str(tier))}</code>")
        else:
            lines.append(
                f"📌 <b>Setup {dir_ru}</b> confirm (closed-bar) · "
                f"<i>delivery заблокирован — см. ниже</i>"
            )
    else:
        badge, label, sub = probe_header(row)
        dump_ok = bool(dump.get("confirmed"))
        long_ok = bool(long_setup.get("confirmed"))
        if dump_ok and label != "SHORT":
            lines.append(
                "⚠️ <i>MTF контекст ≠ Hunt: closed-bar confirm ШОРТ</i>"
            )
        elif long_ok and label != "LONG":
            lines.append(
                "⚠️ <i>MTF контекст ≠ Hunt: closed-bar confirm ЛОНГ</i>"
            )
        if sub:
            lines.append(f"{badge} <b>{html.escape(label)}</b> · <i>{html.escape(sub)}</i>")
        else:
            lines.append(f"{badge} <b>{html.escape(label)}</b>")

    lines.append("")
    lines.extend(
        _hunt_scenario_lines(
            primary_setup,
            direction=primary_dir,
            row=row,
            price=price,
            active=confirmed_direction == primary_dir or not confirmed_direction,
            mtf_sc=primary_mtf,
        )
    )

    show_alt_full = bool(alt_setup.get("confirmed"))
    zones_dup = _entry_zones_overlap(long_setup, dump)
    if not show_alt_full and not zones_dup:
        from hunt_core.deliver.dispatch import display_readiness_score

        alt_ready = display_readiness_score(alt_setup, direction=alt_dir, row=row)
        if alt_ready >= 45.0:
            lines.append("")
            lines.append(_alt_scenario_one_liner(alt_setup, direction=alt_dir, row=row))
    elif show_alt_full:
        lines.append("")
        lines.extend(
            _hunt_scenario_lines(
                alt_setup,
                direction=alt_dir,
                row=row,
                price=price,
                active=confirmed_direction == alt_dir,
                mtf_sc=alt_mtf,
            )
        )
    elif zones_dup:
        lines.append("")
        lines.append(
            "↔ <i>Контр-направление использует ту же зону — один сетап, не два входа</i>"
        )

    from hunt_core.deliver._sections import format_forecast_section

    forecast_block = format_forecast_section(row, primary_direction=primary_dir)
    if forecast_block:
        lines.append("")
        lines.append(forecast_block)

    lines.append("")
    lines.append(f"💬 {html.escape(_brief_reason(row))}")
    lines.append("<i>Watch-only · вход только по confirm системы</i>")
    if added_watch:
        bias = str(lc.get("recommended_bias") or "both")
        lines.append(f"<i>+ watchlist ({html.escape(bias)})</i>")
    return "\n".join(lines)


__all__ = [
    "format_signal_brief_telegram",
]

"""Telegram HTML formatters for hunt entry + follow-ups."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from hunt_watch.alert_explain import invalidate_detail_human
from hunt_watch.pump_history import format_history_telegram
from hunt_core.telegram import TelegramBroadcaster

_PHASE_HUMAN: dict[str, str] = {
    "dump_active": "Активный дамп",
    "dump_initiating": "Начало дампа",
    "dump_imminent": "Дамп неизбежен",
    "dump_setup_forming": "Формируется шорт",
    "dump_confirmed": "Шорт подтверждён",
    "exhaustion_at_high": "Истощение на хаях",
    "exhaustion_watch": "Наблюдение за истощением",
    "distribution": "Распределение",
    "impulse_initiating": "Начало импульса",
    "breakout_arming": "Вооружение пробоя",
    "post_dump_bounce": "Отскок после дампа",
    "accumulation": "Накопление",
    "accumulation_watch": "Наблюдение за накоплением",
    "long_imminent": "Лонг неизбежен",
    "long_setup_forming": "Формируется лонг",
    "long_confirmed": "Лонг подтверждён",
    "no_setup": "Нет сетапа",
    "no_dump_yet": "Нет дампа",
    "no_long_yet": "Нет лонга",
}


def fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    if abs(v) >= 100:
        return f"{v:.3f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    if abs(v) >= 0.01:
        return f"{v:.5f}"
    return f"{v:.6f}"


def phase_human(phase: str) -> str:
    return _PHASE_HUMAN.get(phase, phase)


def phase_badge(phase: str, confirmed: bool, *, direction: str = "short") -> str:
    if confirmed:
        return "🚨"
    if direction == "long":
        return {
            "long_imminent": "🟢",
            "long_setup_forming": "🟡",
            "long_confirmed": "🚨",
            "accumulation_watch": "🔵",
            "no_long_yet": "⚪",
        }.get(phase, "⚪")
    return {
        "dump_imminent": "🔴",
        "dump_setup_forming": "🟠",
        "dump_confirmed": "🚨",
        "exhaustion_watch": "🟡",
        "no_dump_yet": "⚪",
    }.get(phase, "⚪")


def format_setup_lines(
    row: dict[str, Any],
    setup: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any],
    pos: dict[str, Any],
    price: float,
) -> list[str]:
    score_key = "dump_score" if direction == "short" else "long_score"
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    phase = str(setup.get("phase") or "—")
    confirmed = bool(setup.get("confirmed"))
    badge = phase_badge(phase, confirmed, direction=direction)

    def _opt_num(val: Any, *, digits: int = 4) -> str:
        if val is None:
            return "—"
        try:
            return f"{float(val):.{digits}f}"
        except (TypeError, ValueError):
            return "—"

    fuel = _opt_num(setup.get(fuel_key)) if setup.get(fuel_key) is not None else "—"
    score = _opt_num(setup.get(score_key)) if setup.get(score_key) is not None else "—"
    dir_label = "SHORT" if direction == "short" else "LONG"

    def _rsi(key: str) -> str:
        val = (tf.get(key) or {}).get("rsi14")
        return "—" if val is None else f"{val:.0f}"

    div_bits: list[str] = []
    if direction == "short":
        if (tf.get("1h") or {}).get("bearish_rsi_div"):
            div_bits.append("bear1h✓")
        if (tf.get("4h") or {}).get("bearish_rsi_div"):
            div_bits.append("bear4h✓")
    else:
        if (tf.get("1h") or {}).get("bullish_rsi_div"):
            div_bits.append("bull1h✓")
        if (tf.get("4h") or {}).get("bullish_rsi_div"):
            div_bits.append("bull4h✓")
    div_txt = " · " + " ".join(div_bits) if div_bits else ""

    triggers = setup.get("triggers") or []
    trig_txt = html.escape(", ".join(str(t) for t in triggers[:5]))
    if len(triggers) > 5:
        trig_txt += "…"

    ez = setup.get("entry_zone") or [price, price]

    oi = pos.get("oi")
    oi_chg = pos.get("oi_chg_5m")
    fund = pos.get("funding_pct")
    taker = pos.get("taker_5m")
    ls = pos.get("ls_5m")

    if direction == "short":
        level_line = (
            f"Support <code>{fmt_price(setup.get('support_break_level'))}</code> · liq "
            f"<code>{fmt_price(setup.get('resistance_liq'))}</code> · impulse H "
            f"<code>{fmt_price(row.get('impulse_high'))}</code>"
        )
    else:
        level_line = (
            f"Resistance <code>{fmt_price(setup.get('resistance_break_level'))}</code> · support "
            f"<code>{fmt_price(setup.get('support_zone'))}</code> · impulse L "
            f"<code>{fmt_price(row.get('impulse_low'))}</code>"
        )

    lines = [
        f"{badge} <b>{dir_label}</b> · <code>{phase}</code> · "
        f"fuel <code>{fuel}</code> · raw <code>{score}</code>",
        level_line,
        (
            f"Entry <code>{fmt_price(ez[0])}-{fmt_price(ez[1])}</code> · "
            f"SL <code>{fmt_price(setup.get('stop_loss'))}</code> · "
            f"TP1 <code>{fmt_price(setup.get('tp1'))}</code> · "
            f"TP2 <code>{fmt_price(setup.get('tp2'))}</code>"
            + (
                f" · R:R <code>{setup.get('risk_reward')}</code>"
                if setup.get("risk_reward")
                else ""
            )
        ),
        (
            f"RSI 1m/5m/15m/1h/4h: "
            f"<code>{_rsi('1m')}/{_rsi('5m')}/{_rsi('15m')}/{_rsi('1h')}/{_rsi('4h')}</code>"
            f"{div_txt}"
        ),
        (
            f"OI <code>{fmt_price(oi if oi is not None else None)}</code> · "
            f"Δ5m <code>{_opt_num(oi_chg)}</code> · "
            f"fund <code>{_opt_num(fund, digits=3)}%</code> · "
            f"taker5m <code>{_opt_num(taker)}</code> · "
            f"L/S <code>{_opt_num(ls)}</code>"
        ),
        f"Triggers: <code>{trig_txt or '—'}</code>",
    ]
    if confirmed:
        hard = setup.get("confirm_hard") or []
        lines.append(f"<b>✅ CONFIRM</b> {html.escape(', '.join(str(x) for x in hard))}")
    return lines


def _pct_str(a: float, b: float, direction: str) -> str:
    if a <= 0 or b <= 0:
        return ""
    if direction == "short":
        pct = (a - b) / a * 100.0
    else:
        pct = (b - a) / a * 100.0
    return f"+{pct:.1f}%"


def _reason_human(setup: dict[str, Any], *, direction: str, lc_phase: str) -> str:
    phase_txt = phase_human(lc_phase) if lc_phase and lc_phase != "—" else phase_human(
        str(setup.get("phase") or "")
    )
    triggers = setup.get("triggers") or []
    trig_short: list[str] = []
    for t in triggers[:3]:
        ts = str(t)
        if "volume" in ts or "vol" in ts:
            trig_short.append("аномальный объём")
        elif "support" in ts or "break" in ts:
            trig_short.append("пробой поддержки")
        elif "resistance" in ts:
            trig_short.append("пробой сопротивления")
        elif "cascade" in ts or "liq" in ts:
            trig_short.append("каскад ликвидаций")
        elif "rejection" in ts:
            trig_short.append("отбой от уровня")
        elif "rsi" in ts or "div" in ts:
            trig_short.append("RSI-дивергенция")
        elif "funding" in ts:
            trig_short.append("перегрев фандинга")
        elif "oi" in ts:
            trig_short.append("аномалия OI")
        elif "whale" in ts:
            trig_short.append("крупный продавец")
        else:
            trig_short.append(ts.replace("_", " ").split(":")[0])
    trig_txt = ", ".join(dict.fromkeys(trig_short))
    if phase_txt and trig_txt:
        return f"{phase_txt} · {trig_txt}"
    return phase_txt or trig_txt or "—"


def format_entry_telegram(row: dict[str, Any], *, direction: str, confirm_reasons: list[str]) -> str:
    sym = html.escape(str(row["symbol"]).replace("USDT", "-USDT"))
    setup = row["dump"] if direction == "short" else row["long"]
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    score_key = "dump_score" if direction == "short" else "long_score"
    price = float(row.get("price") or 0)
    lc = row.get("lifecycle") or {}
    lc_phase = str(lc.get("phase") or "—")

    badge = "🔴" if direction == "short" else "🟢"
    dir_label = "SHORT" if direction == "short" else "LONG"

    fuel_val = setup.get(fuel_key)
    score_val = setup.get(score_key)
    fuel = float(fuel_val) if fuel_val is not None else 0.0
    fuel_str = f"{fuel:.0f}" if fuel_val is not None else "—"
    score_str = f"{float(score_val):.0f}" if score_val is not None else "—"

    _strong_phases = frozenset(
        {
            "dump_active",
            "exhaustion_at_high",
            "distribution",
            "dump_confirmed",
            "accumulation",
            "impulse_initiating",
            "breakout_arming",
            "long_confirmed",
        }
    )
    if fuel >= 80 and lc_phase in _strong_phases:
        rating = "🔥 СИЛЬНЫЙ"
    elif fuel >= 65 and lc_phase in _strong_phases:
        rating = "✅ УВЕРЕННЫЙ"
    elif fuel >= 50:
        rating = "⚠️ СРЕДНИЙ"
    else:
        rating = "📊 СЛАБЫЙ"

    lifecycle_line = html.escape(phase_human(lc_phase)) if lc_phase != "—" else "—"

    ez = setup.get("entry_zone") or [price, price]
    entry_lo = fmt_price(ez[0])
    entry_hi = fmt_price(ez[1])
    sl = fmt_price(setup.get("stop_loss"))
    tp1 = setup.get("tp1")
    tp2 = setup.get("tp2")
    tp1_pct = _pct_str(price, float(tp1), direction) if tp1 else ""
    tp2_pct = _pct_str(price, float(tp2), direction) if tp2 else ""
    tp1_lbl = setup.get("tp1_label") or ""
    tp2_lbl = setup.get("tp2_label") or ""
    tp1_str = (
        f"<code>{fmt_price(tp1)}</code>"
        + (f" (<b>{tp1_pct}</b>)" if tp1_pct else "")
        + (f" · {tp1_lbl}" if tp1_lbl else "")
    )
    tp2_str = (
        f"<code>{fmt_price(tp2)}</code>"
        + (f" (<b>{tp2_pct}</b>)" if tp2_pct else "")
        + (f" · {tp2_lbl}" if tp2_lbl else "")
    )

    reason = _reason_human(setup, direction=direction, lc_phase=lc_phase)

    header = f"{badge} <b>ВХОД ВЗЯТ · {sym} {dir_label}</b>  {rating}"
    phase_line = f"📌 {lifecycle_line}"
    entry_line = f"📍 Вход: <code>{entry_lo}–{entry_hi}</code>  |  Стоп: <code>{sl}</code>"
    tp_line = f"🎯 TP1: {tp1_str}  |  TP2: {tp2_str}"
    reason_line = f"💡 {html.escape(reason)}"
    score_line = f"📊 Score: <code>{score_str}</code> · Fuel: <code>{fuel_str}</code>"
    footer = "<i>Signal-only · closed 5m/1m confirm · открывай сделку вручную</i>"

    hist = format_history_telegram(row.get("pump_history"))
    hist_line = f"{html.escape(hist)}\n" if hist else ""

    return f"{header}\n{phase_line}\n{entry_line}\n{tp_line}\n{reason_line}\n{score_line}\n{hist_line}\n{footer}"


def _duration_str(opened: str) -> str:
    try:
        start = datetime.fromisoformat(opened.replace(" ", "T"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        delta = datetime.now(UTC) - start
        total_m = int(delta.total_seconds() // 60)
        h, m = divmod(total_m, 60)
        if h > 0:
            return f"{h}ч {m}м"
        return f"{m}м"
    except Exception:
        return "—"


def format_followup_telegram(followup: Any, row: dict[str, Any]) -> str:
    sym = html.escape(str(followup.symbol).replace("USDT", "-USDT"))
    direction = followup.direction.upper()
    price = fmt_price(followup.price)
    lc = row.get("lifecycle") or {}
    payload = followup.payload if isinstance(followup.payload, dict) else {}
    event = followup.event

    sl = fmt_price(payload.get("stop_loss"))
    tp1_lvl = fmt_price(payload.get("tp1"))
    tp2_lvl = fmt_price(payload.get("tp2"))
    entry_lo = payload.get("entry_lo")
    entry_hi = payload.get("entry_hi")
    entry_zone = (
        f"{fmt_price(entry_lo)}–{fmt_price(entry_hi)}"
        if entry_lo is not None and entry_hi is not None
        else "—"
    )
    opened_raw = str(payload.get("opened_at") or "")[:19].replace("T", " ")
    msg_id = payload.get("entry_message_id")
    entry_ref = f"Вход {entry_zone}"
    if msg_id:
        entry_ref += f" · сигнал TG <code>#{msg_id}</code>"

    reason_raw = str(payload.get("reason") or "")
    detail_human = invalidate_detail_human(str(followup.detail or ""), reason=reason_raw)

    if event == "fix_profit_tp1":
        fix_pct = int(payload.get("partial_fixed_pct") or 50)
        new_sl = fmt_price(payload.get("stop_loss"))
        tp1_pct_val = payload.get("tp1")
        entry_price_est = entry_lo or 0
        tp1_pct_str = ""
        if tp1_pct_val and entry_price_est:
            try:
                if direction == "SHORT":
                    tp1_pct = (float(entry_price_est) - float(tp1_pct_val)) / float(entry_price_est) * 100.0
                else:
                    tp1_pct = (float(tp1_pct_val) - float(entry_price_est)) / float(entry_price_est) * 100.0
                tp1_pct_str = f" +{tp1_pct:.1f}%"
            except Exception:
                pass
        return (
            f"✅ <b>TP1 достигнут{tp1_pct_str} · {sym} {direction}</b>\n"
            f"🔒 Зафиксируй <b>{fix_pct}%</b> позиции · Стоп перенесён на безубыток <code>{new_sl}</code>\n"
            f"🎯 Следующая цель: TP2 <code>{tp2_lvl}</code>\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "fix_profit_tp2":
        duration = _duration_str(opened_raw)
        skipped = bool(payload.get("tp1_skipped"))
        extra = " (TP1 пролёт)" if skipped else ""
        return (
            f"📋 <b>Закрыт {sym} {direction}{extra}</b>\n"
            f"💰 PnL: TP2 <code>{tp2_lvl}</code> · Длит: {duration}\n"
            f"📌 Причина: Достигнут TP2\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "invalidate":
        duration = _duration_str(opened_raw)
        reason_label_map = {
            "stop_hit": "Стоп-лосс пробит",
            "tp1": "Закрыто по TP1",
            "tp2": "Закрыто по TP2",
            "bounce_invalidate": "Lifecycle: отскок — шорт отменён",
            "time_stall": "Нет прогресса за 8ч — тезис не сработал",
            "bias_flip": "Lifecycle сменил bias против позиции",
            "support_lost": "Потеря поддержки (лонг)",
        }
        reason_str = reason_label_map.get(reason_raw, html.escape(detail_human))
        return (
            f"📋 <b>Закрыт {sym} {direction}</b>\n"
            f"📌 Причина: {reason_str}\n"
            f"⏱ Длит: {duration}\n"
            f"{entry_ref}\n"
            f"Уровни: SL <code>{sl}</code> · TP1 <code>{tp1_lvl}</code> · TP2 <code>{tp2_lvl}</code>\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    if event == "stop_warning":
        return (
            f"⚠️ <b>СТОП РЯДОМ · {sym} {direction}</b>\n"
            f"Цена <code>{price}</code> близко к SL <code>{sl}</code>\n"
            f"Реши: держать или фиксировать вручную.\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    badges = {"phase_change": "🔄", "avg_zone": "➕"}
    titles = {"phase_change": "PHASE CHANGE", "avg_zone": "AVG ZONE"}
    badge = badges.get(event, "📣")
    title = titles.get(event, event)
    lc_phase_now = html.escape(phase_human(str(lc.get("phase") or "—")))
    return (
        f"{badge} <b>{title}</b>\n"
        f"{sym} · <code>{direction}</code> · цена <code>{price}</code>\n"
        f"{html.escape(detail_human)}\n"
        f"{entry_ref}\n"
        f"SL <code>{sl}</code> · TP1 <code>{tp1_lvl}</code> · TP2 <code>{tp2_lvl}</code>\n"
        f"Фаза: {lc_phase_now}\n"
        f"<i>Hunt follow-up · не auto-trade</i>"
    )


def split_telegram(text: str, *, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    chunk = ""
    for block in text.split("\n\n"):
        candidate = f"{chunk}\n\n{block}".strip() if chunk else block
        if len(candidate) <= limit:
            chunk = candidate
            continue
        if chunk:
            parts.append(chunk)
        chunk = block
    if chunk:
        parts.append(chunk)
    return parts or [text[:limit]]


async def send_telegram_chunks(
    broadcaster: TelegramBroadcaster,
    text: str,
    *,
    log_key: str,
    log: Any,
) -> bool:
    ok = True
    for idx, part in enumerate(split_telegram(text)):
        result = await broadcaster.send_html(part)
        if result.status != "sent":
            log.warning(
                f"{log_key}_failed",
                part=idx + 1,
                status=result.status,
                reason=result.reason,
            )
            ok = False
        else:
            log.info(f"{log_key}_sent", part=idx + 1, message_id=result.message_id)
    return ok

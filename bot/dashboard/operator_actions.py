"""Operator control actions - Telegram console and dashboard parity."""

from __future__ import annotations

import asyncio
import html
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.runtime.errors import DEFENSIVE_EXC

from ..domain.config import _ALL_SETUP_IDS
from ..domain.schemas import UniverseSymbol
from .tracking_view import resolve_mark_price, serialize_tracking_signal
from .user_summary import reject_reason_ru

if TYPE_CHECKING:
    from ..runtime.bot import SignalBot


def normalize_operator_symbol(raw: str, *, quote_asset: str = "USDT") -> str | None:
    """Parse BTC, btc/usdt, BTCUSDT → BTCUSDT."""
    text = str(raw or "").strip().upper().replace("/", "").replace("-", "").replace("_", "")
    if not text:
        return None
    if len(text) == 8 and all(ch in "0123456789ABCDEF" for ch in text):
        return None
    quote = str(quote_asset or "USDT").strip().upper()
    if text.endswith(quote):
        return text
    if text.isalnum() and 2 <= len(text) <= 12:
        return f"{text}{quote}"
    return None


def is_tracking_ref(token: str) -> bool:
    text = str(token or "").strip().upper()
    return len(text) == 8 and all(ch in "0123456789ABCDEF" for ch in text)


def find_shortlist_item(bot: Any, symbol: str) -> UniverseSymbol | None:
    sym = symbol.upper()
    for bucket in (
        getattr(bot, "_shortlist", None),
        getattr(bot, "_last_live_shortlist", None),
    ):
        for item in bucket or []:
            if str(getattr(item, "symbol", "")).upper() == sym:
                return item
    return None


def build_adhoc_universe_symbol(bot: Any, symbol: str) -> UniverseSymbol | None:
    """Build a runnable UniverseSymbol for one-off operator analyze."""
    sym = symbol.upper()
    service = bot._get_shortlist_service()
    base_asset, quote_asset = service.extract_symbol_assets(sym)
    if not base_asset or not quote_asset:
        return None
    meta_by_symbol = getattr(bot, "_symbol_meta_by_symbol", {}) or {}
    if meta_by_symbol and sym not in meta_by_symbol:
        return None
    meta = meta_by_symbol.get(sym)
    contract_type = str(getattr(meta, "contract_type", "") or "PERPETUAL").upper()
    onboard_date_ms = int(getattr(meta, "onboard_date_ms", 0) or 0)
    existing = find_shortlist_item(bot, sym)
    if existing is not None:
        return existing
    last_price = 0.0
    mark = resolve_mark_price(bot, sym)
    if mark is not None:
        last_price = mark
    return UniverseSymbol(
        symbol=sym,
        base_asset=base_asset,
        quote_asset=quote_asset,
        contract_type=contract_type,
        status="TRADING",
        onboard_date_ms=onboard_date_ms,
        quote_volume=0.0,
        price_change_pct=0.0,
        last_price=last_price,
        shortlist_bucket="operator_adhoc",
        shortlist_score=0.0,
        shortlist_reasons=("operator_analyze",),
        seed_source="operator_adhoc",
        strategy_fits=tuple(_ALL_SETUP_IDS),
    )


async def lookup_symbol_signals(bot: Any, symbol: str) -> list[dict[str, Any]]:
    repo = getattr(bot, "_modern_repo", None)
    if repo is None:
        return []
    sym = symbol.upper()
    try:
        open_rows = await repo.get_active_signals(symbol=sym)
        closed_rows = await repo.get_active_signals(
            symbol=sym,
            status="closed",
            include_closed=True,
        )
    except DEFENSIVE_EXC:
        return []
    rows = list(open_rows or [])
    seen = {str(r.get("tracking_id") or "") for r in rows}
    for row in closed_rows or []:
        tid = str(row.get("tracking_id") or "")
        if tid and tid not in seen:
            rows.append(row)
            seen.add(tid)
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows[:12]


async def lookup_signal_by_ref(bot: Any, token: str) -> dict[str, Any] | None:
    """Find signal by tracking_ref (hex) or symbol (latest open/closed)."""
    repo = getattr(bot, "_modern_repo", None)
    if repo is None:
        return None
    needle = str(token or "").strip().upper()
    if not needle:
        return None

    if not is_tracking_ref(needle):
        sym = normalize_operator_symbol(needle, quote_asset=bot.settings.universe.quote_asset)
        if sym:
            rows = await lookup_symbol_signals(bot, sym)
            return rows[0] if rows else None

    try:
        all_open = await repo.get_active_signals(include_closed=True)
    except DEFENSIVE_EXC:
        return None
    for row in sorted(all_open or [], key=lambda r: str(r.get("created_at") or ""), reverse=True):
        ref = str(row.get("tracking_ref") or "").upper()
        tid = str(row.get("tracking_id") or "").upper()
        if ref == needle or needle in tid:
            return row
    return None


async def symbol_rejection_summary(
    live_data: Any, symbol: str, *, limit: int = 5
) -> list[tuple[str, int]]:
    if live_data is None:
        return []
    try:
        payload = await live_data.rejections(limit=200, max_rows=20_000)
    except DEFENSIVE_EXC:
        return []
    sym = symbol.upper()
    counter: Counter[str] = Counter()
    for row in payload.get("rows") or []:
        if str(row.get("symbol") or "").upper() != sym:
            continue
        reason = str(row.get("reason") or row.get("reason_code") or row.get("stage") or "?")
        counter[reason] += 1
    return counter.most_common(limit)


async def format_symbol_lookup_html(bot: Any, symbol: str, rows: list[dict[str, Any]]) -> str:
    sym = html.escape(symbol.upper())
    item = find_shortlist_item(bot, symbol)
    mark = resolve_mark_price(bot, symbol.upper())
    lines = [f"<b>{sym}</b>"]
    if item is not None:
        lines.append(
            f"Shortlist: <code>{html.escape(str(item.shortlist_bucket or '-'))}</code> · "
            f"score <code>{item.shortlist_score or 0:.3f}</code> · "
            f"fits <code>{len(item.strategy_fits)}</code>"
        )
    else:
        lines.append("<i>Не в текущем shortlist</i> - /analyze для разового прогона")
    if mark is not None:
        lines.append(f"Mark: <code>{mark}</code>")
    if not rows:
        lines.append("Открытых/недавних сигналов в БД нет.")
    else:
        lines.append(f"<b>Сигналы ({len(rows)})</b>")
        for row in rows[:6]:
            payload = serialize_tracking_signal(row, bot)
            setup = html.escape(str(payload.get("setup_id") or "?"))
            direction = html.escape(str(payload.get("direction") or "?"))
            status = html.escape(str(payload.get("status") or "?"))
            ref = html.escape(str(payload.get("tracking_ref") or "?"))
            progress = html.escape(str(payload.get("progress_label") or ""))
            lines.append(f"• {setup} {direction} · <code>{status}</code> · ref <code>{ref}</code>")
            if progress:
                lines.append(f"  {progress}")
    live_data = getattr(getattr(bot, "dashboard", None), "_live_data", None)
    rejections = await symbol_rejection_summary(live_data, symbol)
    if rejections:
        lines.append("<b>Top rejections (session)</b>")
        for reason, count in rejections:
            lines.append(f"• {html.escape(reject_reason_ru(reason))}: <code>{count}</code>")
    lines.append("<i>/signal REF · /analyze · /review</i>")
    return "\n".join(lines)


def format_signal_detail_html(bot: Any, row: dict[str, Any]) -> str:
    payload = serialize_tracking_signal(row, bot)
    sym = html.escape(str(payload.get("symbol") or "?"))
    ref = html.escape(str(payload.get("tracking_ref") or "?"))
    setup = html.escape(str(payload.get("setup_id") or "?"))
    direction = html.escape(str(payload.get("direction") or "?"))
    status = html.escape(str(payload.get("status") or "?"))
    lines = [
        f"<b>{sym}</b> · ref <code>{ref}</code>",
        f"{setup} · {direction} · <code>{status}</code>",
    ]
    entry = payload.get("entry_mid") or payload.get("entry_price")
    stop = payload.get("stop_price")
    tp1 = payload.get("tp1_price")
    tp2 = payload.get("tp2_price")
    if entry is not None:
        lines.append(f"Entry: <code>{entry}</code>")
    el, eh = row.get("entry_low"), row.get("entry_high")
    if el is not None and eh is not None:
        lines.append(f"Zone: <code>{el}</code> - <code>{eh}</code>")
    if stop is not None:
        lines.append(f"SL: <code>{stop}</code>")
    if tp1 is not None:
        tps = f"TP1 <code>{tp1}</code>"
        if tp2 is not None:
            tps += f" · TP2 <code>{tp2}</code>"
        lines.append(tps)
    current = payload.get("current_price")
    if current is not None:
        lines.append(f"Price: <code>{current}</code>")
    progress = payload.get("progress_label")
    if progress:
        lines.append(f"Progress: {html.escape(str(progress))}")
    zone_at = row.get("entry_zone_touched_at")
    act_at = row.get("activated_at")
    if zone_at:
        lines.append(f"Zone touched: <code>{html.escape(str(zone_at)[:19])}</code>")
    if act_at:
        lines.append(f"Activated: <code>{html.escape(str(act_at)[:19])}</code>")
    close = row.get("close_reason")
    if close:
        lines.append(f"Close: <code>{html.escape(str(close))}</code>")
    score = row.get("score")
    rr = row.get("risk_reward")
    if score is not None:
        lines.append(f"Score: <code>{float(score):.3f}</code> · RR <code>{rr}</code>")
    return "\n".join(lines)


def format_action_result_html(action: str, result: dict[str, Any]) -> str:
    title = html.escape(action)
    if result.get("error"):
        return f"<b>{title}</b>\n❌ {html.escape(str(result['error']))}"
    lines = [f"<b>{title}</b> ✅"]
    for key, value in sorted(result.items()):
        if key in {"error", "ok"}:
            continue
        lines.append(f"{html.escape(str(key))}: <code>{html.escape(str(value))}</code>")
    return "\n".join(lines)


async def action_refresh_shortlist(bot: SignalBot) -> dict[str, Any]:
    started = datetime.now(UTC)
    try:
        shortlist = await bot._do_refresh_shortlist()
        await bot._sync_ws_tracked_symbols()
        preload_task = asyncio.create_task(
            bot._preload_shortlist_frames(),
            name="operator_preload_frames",
        )
        bot._background_tasks.add(preload_task)
        preload_task.add_done_callback(bot._background_tasks.discard)
    except DEFENSIVE_EXC as exc:
        return {"error": str(exc)}
    elapsed = (datetime.now(UTC) - started).total_seconds()
    source = str(getattr(bot, "_shortlist_source", "") or "-")
    return {
        "ok": True,
        "size": len(shortlist),
        "source": source,
        "elapsed_s": round(elapsed, 1),
    }


async def action_refresh_market_context(bot: SignalBot) -> dict[str, Any]:
    started = datetime.now(UTC)
    try:
        async with bot._shortlist_lock:
            shortlist = list(bot._shortlist)
        if not shortlist:
            shortlist = await bot._do_refresh_shortlist()
        await bot._update_memory_market_context(shortlist)
        updater = getattr(bot, "_market_context_updater", None)
        if updater is not None:
            await updater.build_market_state_html(force=True)
    except DEFENSIVE_EXC as exc:
        return {"error": str(exc)}
    elapsed = (datetime.now(UTC) - started).total_seconds()
    regime = getattr(bot, "market_regime", None)
    last = getattr(regime, "_last_result", None) if regime is not None else None
    return {
        "ok": True,
        "shortlist": len(shortlist),
        "regime": getattr(last, "regime", None) if last else None,
        "btc_bias": getattr(last, "btc_bias", None) if last else None,
        "elapsed_s": round(elapsed, 1),
    }


async def action_scan_market(bot: SignalBot) -> dict[str, Any]:
    started = datetime.now(UTC)
    try:
        stats = await bot._run_emergency_cycle()
    except DEFENSIVE_EXC as exc:
        return {"error": str(exc)}
    elapsed = (datetime.now(UTC) - started).total_seconds()
    return {
        "ok": True,
        "shortlist_size": stats.get("shortlist_size"),
        "candidates": stats.get("candidates"),
        "delivered": stats.get("delivered"),
        "rejected": stats.get("rejected"),
        "elapsed_s": round(elapsed, 1),
    }


async def action_analyze_symbol(bot: SignalBot, symbol: str) -> dict[str, Any]:
    sym = symbol.upper()
    item = build_adhoc_universe_symbol(bot, sym)
    if item is None:
        return {"error": f"symbol_not_found:{sym}"}
    started = datetime.now(UTC)
    try:
        tracking_events = await bot.tracker.review_open_signals_for_symbol(sym, dry_run=False)
        if tracking_events:
            await bot._deliver_tracking(tracking_events)
        async with bot._shortlist_lock:
            shortlist_size = len(bot._shortlist)
        await bot._get_cycle_runner().execute_symbol_cycle(
            symbol=sym,
            item=item,
            interval="15m",
            trigger="operator_analyze",
            event_ts=datetime.now(UTC),
            tracking_events=tracking_events,
            shortlist_size=max(shortlist_size, 1),
        )
    except DEFENSIVE_EXC as exc:
        return {"error": str(exc)}
    elapsed = (datetime.now(UTC) - started).total_seconds()
    return {"ok": True, "symbol": sym, "elapsed_s": round(elapsed, 1)}


async def action_review_tracking(bot: SignalBot, symbol: str) -> dict[str, Any]:
    sym = symbol.upper()
    started = datetime.now(UTC)
    try:
        events = await bot.tracker.review_open_signals_for_symbol(sym, dry_run=False)
        if events:
            await bot._deliver_tracking(events)
    except DEFENSIVE_EXC as exc:
        return {"error": str(exc)}
    elapsed = (datetime.now(UTC) - started).total_seconds()
    return {
        "ok": True,
        "symbol": sym,
        "events": len(events),
        "elapsed_s": round(elapsed, 1),
    }

"""On-demand symbol analysis for /signal — rate-limited, separate REST client."""
from __future__ import annotations



import asyncio
import html
import logging
from typing import Any

LOG = logging.getLogger("hunt_core.runtime.symbol_probe")

from hunt_core.data.collect import safe_fetch
from hunt_core.runtime.tick_assembly import snapshot_symbol
from hunt_core.domain.config import load_settings
from hunt_core.features.prepare import _prepare_frame, min_required_bars
from hunt_core.market import HuntCcxtClient
from hunt_core.deliver.telegram import TelegramBroadcaster

from hunt_core.gate.delivery import evaluate_alert_gate, evaluate_formation
from hunt_core.analysis.deep_signal import (
    btc_market_context,
    forming_confirm_gaps,
    probe_header,
    resolve_trade_direction,
    scenario_summary,
)
from hunt_core.track.events import append_audit_log, audit_probe_row, backtest_levels_on_bars
from hunt_core.track.tracker import load_tracker_state
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.params.store import effective_hunt_params
from hunt_core.data.universe import add_to_watchlist, register_signal_notify

_STAGGER_MS = 150
_PROBE_TIMEOUT_S = 240.0
_PINNED_PROBE_TIMEOUT_S = 360.0


def normalize_symbol(raw: str) -> str:
    sym = raw.strip().upper().replace("/", "").replace("-", "")
    if not sym:
        return ""
    if sym.endswith("USDC"):
        return sym
    return sym if sym.endswith("USDT") else f"{sym}USDT"


def parse_symbol_text(text: str) -> str:
    """Plain chat text → symbol (btc, BEAT, ETHUSDT) without /command."""
    raw = text.strip().upper()
    if not raw or raw.startswith("/"):
        return ""
    raw = raw.replace("/", "").replace("-", "")
    if " " in raw:
        parts = [p for p in raw.split() if p]
        if len(parts) == 1:
            raw = parts[0]
        elif parts[0] in {"SIGNAL", "SIG", "СИГНАЛ"} and len(parts) >= 2:
            raw = parts[1]
        else:
            return ""
    return normalize_symbol(raw)


def resolve_direction(row: dict[str, Any]) -> tuple[str, dict[str, Any], float, list[str]]:
    return resolve_trade_direction(row)


def _best_direction(row: dict[str, Any]) -> tuple[str, dict[str, Any], float]:
    direction, setup, fuel, _ = resolve_trade_direction(row)
    return direction, setup, fuel


def _is_hunt_anomaly(row: dict[str, Any], *, symbol: str) -> bool:
    cal = effective_hunt_params(symbol)
    sess = row.get("session") or {}
    chg = abs(float(row.get("chg_24h_pct") or 0))
    rng = float(sess.get("range_pct_24h") or 0)
    if bool(row.get("young_listing")):
        return True
    return chg >= cal.anomaly_min_chg_24h_pct or rng >= cal.anomaly_min_range_24h_pct


def format_signal_probe_telegram(
    row: dict[str, Any], *, added_watch: bool, compact: bool = False
) -> str:
    """User-facing /signal reply when closed-bar confirm is not yet present.

    ``compact`` drops blocks already rendered by the pinned deep-analysis header
    (liquidity scenarios, volume profile, regime) so the two are not concatenated
    with duplicates.
    """
    from hunt_core.analysis.deep_signal import (
        build_liquidity_scenarios,
        format_liquidity_scenarios_telegram,
    )
    from hunt_core.deliver.dispatch import (
        readiness_short_for_setup,
    )
    from hunt_core.deliver.telegram import fmt_price, format_setup_lines

    sym = html.escape(str(row.get("symbol", "?")).replace("USDT", "-USDT"))
    if row.get("error"):
        return f"⚠️ <b>/signal {sym}</b>\n<code>{html.escape(str(row['error']))}</code>"

    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    lc = row.get("lifecycle") or {}
    direction, setup, best_fuel, btc_notes = resolve_direction(row)
    other_dir = "long" if direction == "short" else "short"
    other_setup = long_setup if direction == "short" else dump

    short_ok = bool(dump.get("confirmed"))
    long_ok = bool(long_setup.get("confirmed"))
    if short_ok or long_ok:
        return ""  # caller uses full _format_telegram

    price = float(row.get("price") or 0)
    tf = row.get("timeframes") or {}
    pos = row.get("market") or row.get("positioning") or {}
    badge, dir_label, header_sub = probe_header(row)
    bias = str(lc.get("recommended_bias") or "both")
    watch_bias = bias if bias in {"short", "long", "both"} else "both"
    btc_ctx = row.get("btc_context") or {}
    regime = row.get("regime") or {}
    corr = regime.get("btc_corr_1h")

    verdict = row.get("pinned_verdict")
    verdict_kind = getattr(verdict, "kind", None) if verdict is not None else None
    if compact and verdict_kind == "sideways":
        header = (
            f"🔍 <b>/signal {sym}</b> · контекст {badge} <b>{dir_label}</b> "
            f"(advisory · вердикт боковик)"
        )
    elif header_sub:
        header = f"🔍 <b>/signal {sym}</b> · {badge} <b>{dir_label}</b> · <i>{html.escape(header_sub)}</i>"
    else:
        header = f"🔍 <b>/signal {sym}</b> · приоритет: {badge} <b>{dir_label}</b>"

    lines = [
        header,
        scenario_summary(
            direction=direction,
            setup=setup,
            fuel=best_fuel,
            lc=lc,
            confirmed=False,
            row=row,
        ),
        (
            f"Статус: <code>ждём closed-bar confirm</code> · "
            f"{readiness_short_for_setup(setup, direction=direction, row=row)} vs {other_dir} "
            f"<code>{readiness_short_for_setup(other_setup, direction=other_dir, row=row)}</code>"
        ),
        (
            f"Цена <code>{fmt_price(price)}</code> · 24h "
            f"<code>{row.get('chg_24h_pct')}%</code> · lifecycle "
            f"<code>{html.escape(str(lc.get('phase') or '—'))}</code>"
        ),
    ]
    lc_phase = str(lc.get("phase") or "")
    if lc_phase == "no_setup":
        oi = pos.get("oi")
        oi_usd = float(oi) * price if oi is not None and price > 0 else None
        if oi_usd is not None and oi_usd >= 1_000_000:
            oi_txt = f"${oi_usd / 1_000_000:.1f}M"
        elif oi_usd is not None:
            oi_txt = f"${oi_usd:,.0f}" if oi_usd else "—"
        else:
            oi_txt = "—"
        lines.extend(
            [
                (
                    "<i>lifecycle=no_setup: MTF bias и fuel — справочно; "
                    "уровни входа скрыты до structural confirm.</i>"
                ),
                (
                    f"Контекст fuel: short <code>{float(dump.get('dump_fuel') or 0):.0f}</code> · "
                    f"long <code>{float(long_setup.get('long_fuel') or 0):.0f}</code> · "
                    f"OI ≈ <code>{oi_txt}</code>"
                ),
            ]
        )
    else:
        lines.append(
            "<i>Hunt анализирует pump/dump по REST+WS; confirm = закрытый 5m/1m бар. "
            "Ниже — уровни, OI, funding и триггеры для ручного решения.</i>"
        )
        lines.extend(
            format_setup_lines(
                row,
                setup,
                direction=direction,
                tf=tf,
                pos=pos,
                price=price,
                suppress_context=compact,
            )
        )
    if regime and not compact:
        lines.append(
            "Regime: "
            f"<code>{html.escape(str(regime.get('regime_4h') or '—'))}</code>/"
            f"<code>{html.escape(str(regime.get('regime_1h') or '—'))}</code> · "
            f"structure <code>{html.escape(str(regime.get('structure_1h') or '—'))}</code>"
        )
    if sym != "BTC-USDT" and (btc_ctx or corr is not None):
        b1 = btc_ctx.get("btc_chg_1h_pct")
        trend = btc_ctx.get("btc_trend") or "—"
        lines.append(
            "BTC: "
            f"1h <code>{b1 if b1 is not None else '—'}%</code> "
            f"<code>{html.escape(str(trend))}</code> · "
            f"corr <code>{corr if corr is not None else '—'}</code>"
        )
        for note in btc_notes:
            lines.append(f"<i>{html.escape(note)}</i>")
    if lc_phase != "no_setup":
        gaps = forming_confirm_gaps(setup, direction=direction, tf=tf, row=row, price=price)
        if gaps:
            lines.append(
                "До confirm нужно: "
                f"<code>{html.escape(', '.join(gaps[:6]))}</code>"
            )
        form = evaluate_formation(setup, direction=direction, symbol=str(row.get("symbol") or ""), lifecycle=lc)
        lines.append(f"Формирование: <i>{html.escape(form.message)}</i>")
    # Liquidity scenarios — deep pinned header renders liquidity + POC taxonomy;
    # memecoin pump/dump scan skips POC block (see poc_level_scenarios).
    if not compact:
        if row.get("_deep_analysis"):
            from hunt_core.analysis.deep_signal import (
                build_poc_level_scenarios,
                format_poc_level_scenarios_telegram,
            )

            poc_pack = row.get("poc_level_scenarios") or build_poc_level_scenarios(row)
            poc_block = format_poc_level_scenarios_telegram(poc_pack)
            if poc_block:
                lines.extend(["", poc_block])
        elif not row.get("_pinned_reference"):
            if not row.get("liquidity_scenarios"):
                row["liquidity_scenarios"] = build_liquidity_scenarios(row)
            liq_block = format_liquidity_scenarios_telegram(row["liquidity_scenarios"])
            if liq_block:
                lines.extend(["", liq_block])
    if bool(setup.get("confirmed")):
        gate = evaluate_alert_gate(
            setup,
            direction=direction,
            symbol=str(row.get("symbol") or ""),
            lifecycle=lc,
            row=row,
        )
        if gate.ok:
            lines.append("✅ Все гейты доставки пройдены")
        else:
            lines.append(f"⛔ Алерт заблокирован: <i>{html.escape(gate.message)}</i>")
    if added_watch:
        lines.append(
            f"✅ Добавлен в watchlist (<code>{watch_bias}</code>) — пришлю сигнал при confirm."
        )
    else:
        lines.append(
            "ℹ️ Уже в watchlist — уведомлю при confirm (<code>dump_confirmed</code> / "
            "<code>long_confirmed</code>)."
        )
    lines.append("<i>On-demand probe · staggered REST · не прерывает hunt loop</i>")
    return "\n".join(lines)


async def probe_symbol_signal(
    symbol: str,
    *,
    stagger_ms: int = _STAGGER_MS,
    auto_watchlist: bool = True,
    probe_kind: str = "signal",
) -> dict[str, Any]:
    """Full hunt analysis for one symbol using an isolated REST client.

    ``probe_kind="catalog"`` — shadow scan for /signals: no watchlist, no tracker
    backtest, lighter enrichments. ``probe_kind="signal"`` — /signal point query.
    """
    sym = normalize_symbol(symbol)
    if not sym:
        return {"symbol": symbol, "error": "empty_symbol"}

    catalog_probe = probe_kind == "catalog"
    if catalog_probe:
        auto_watchlist = False

    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    client = HuntCcxtClient.from_settings(settings)
    await client.load_markets()
    try:
        premium_all = await safe_fetch(
            client.fetch_premium_index_all(), context="premium_index_all"
        ) or {}
        await asyncio.sleep(stagger_ms / 1000.0)
        funding_info_all = await safe_fetch(
            client.fetch_funding_info_all(), context="funding_info_all"
        ) or {}
        await asyncio.sleep(stagger_ms / 1000.0)
        exchange_list = await safe_fetch(
            client.fetch_exchange_symbols(), context="exchange_symbols"
        ) or []
        exchange_by_sym = {r.symbol: r for r in exchange_list}
        await asyncio.sleep(stagger_ms / 1000.0)
        ticker_raw = await safe_fetch(client.fetch_ticker_24h(), context="ticker_24h") or []
        ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}

        btc_work_1h = None
        btc_df = await safe_fetch(
            client.fetch_klines_cached("BTCUSDT", "1h", limit=500),
            context="btc_klines_1h",
        )
        if btc_df is not None and not btc_df.is_empty():
            btc_work_1h = _prepare_frame(btc_df)

        row = await asyncio.wait_for(
            snapshot_symbol(
                client,
                settings,
                minimums,
                sym,
                watch_mode="both",
                prev_oi=None,
                premium_all=premium_all,
                funding_info_all=funding_info_all,
                btc_work_1h=btc_work_1h,
                exchange_by_sym=exchange_by_sym,
                ticker_by_sym=ticker_by_sym,
                ws_feed=None,
                spot_companion=None,
                stagger_klines_ms=stagger_ms,
            ),
            timeout=_PROBE_TIMEOUT_S,
        )
        if btc_work_1h is not None:
            row["btc_context"] = btc_market_context(btc_work_1h)
        # Ручной /signal (и прочие explicit-пробы) дают полный направленный разбор
        # по ЛЮБОМУ символу, включая якоря BTC/ETH/XAU/XAG вне аномалии. Meme-only
        # фильтр остаётся только в пассивном сканере (watch.py). Якорь при низкой
        # волатильности помечается явным флагом, но НЕ блокируется.
        if sym in PINNED_SYMBOLS:
            row["_pinned_reference"] = True
            row["_low_volatility_anchor"] = not _is_hunt_anomaly(row, symbol=sym)
        if not catalog_probe:
            # MTF confluence (pinned + any explicit /signal symbol with frames)
            try:
                from hunt_core.analysis.deep_signal import build_liquidity_scenarios
                from hunt_core.confluence.mtf import build_mtf_confluence

                tf = row.get("timeframes") or {}
                price = float(row.get("price") or 0)
                if tf and price > 0:
                    row["liquidity_scenarios"] = build_liquidity_scenarios(row)
                    row["mtf"] = build_mtf_confluence(
                        sym, tf, price, market=row.get("market"), row=row
                    )
            except Exception as _mtf_exc:
                LOG.warning("mtf_confluence_failed | sym=%s error=%s", sym, _mtf_exc)
            # Cross-exchange (Binance-listed symbol vs Bybit/OKX/Bitget)
            try:
                from hunt_core.market.cross import attach_cross_fields

                cx = await asyncio.wait_for(
                    client.fetch_cross_exchange_snapshot(sym),
                    timeout=30.0,
                )
                if isinstance(cx, dict):
                    attach_cross_fields(row, cx)
            except Exception as _cx_exc:
                LOG.warning("cross_exchange_failed | sym=%s error=%s", sym, _cx_exc)
            if sym in PINNED_SYMBOLS:
                try:
                    from hunt_core.market.cross import attach_cross_microstructure

                    await attach_cross_microstructure(client, row)
                    cx_micro = row.get("cross_microstructure") or {}
                    cross_walls = cx_micro.get("book_walls")
                    if isinstance(cross_walls, dict) and cross_walls.get("bid_levels"):
                        row["book_walls"] = cross_walls
                except Exception as _cm_exc:
                    LOG.warning("cross_microstructure_failed | sym=%s error=%s", sym, _cm_exc)
        audit_source = "signals_cmd" if catalog_probe else "signal_cmd"
        audit = audit_probe_row(row, source=audit_source)
        if not catalog_probe:
            bt = await _tracker_levels_backtest(client, sym)
            if bt:
                audit["tracker_backtest"] = bt
        append_audit_log(audit)
        row["_signal_audit"] = audit
        if catalog_probe:
            row["_probe_kind"] = "catalog"
        if auto_watchlist and not row.get("error"):
            dump = row.get("dump") or {}
            long_setup = row.get("long") or {}
            lc = row.get("lifecycle") or {}
            bias = str(lc.get("recommended_bias") or "both")
            watch_bias = bias if bias in {"short", "long", "both"} else "both"
            fuel = max(
                float(dump.get("dump_fuel") or 0),
                float(long_setup.get("long_fuel") or 0),
            )
            added = add_to_watchlist(
                sym,
                source="signal_cmd",
                hunt_score=fuel,
                watch_bias=watch_bias,
                note=f"signal_probe phase={dump.get('phase')}",
            )
            direction, _, _, _ = resolve_trade_direction(row)
            setup = dump if direction == "short" else long_setup
            notify_phase = str(setup.get("phase") or "dump_confirmed" if direction == "short" else "long_confirmed")
            register_signal_notify(
                sym,
                direction=direction,
                phase=notify_phase,
            )
            row["_watchlist_added"] = added
        return row
    finally:
        await client.close()


async def probe_symbol_catalog(
    symbol: str,
    *,
    stagger_ms: int = 120,
) -> dict[str, Any]:
    """Shadow catalog snapshot for /signals — no watchlist or tracker side effects."""
    return await probe_symbol_signal(
        symbol,
        stagger_ms=stagger_ms,
        probe_kind="catalog",
        auto_watchlist=False,
    )


async def probe_pinned_deep(
    symbol: str,
    *,
    stagger_ms: int = 200,
    auto_watchlist: bool = True,
) -> dict[str, Any]:
    """Extended REST + full prepare + microstructure for pinned anchors."""
    import os

    from hunt_core.analysis.pinned_deep import build_pinned_verdict
    from hunt_core.features.microstructure import build_microstructure_context
    from hunt_core.data.universe import cache_is_fresh, load_pinned_cache

    sym = normalize_symbol(symbol)
    old_full = os.environ.get("HUNT_FULL_PREPARE")
    os.environ["HUNT_FULL_PREPARE"] = "1"
    try:
        row = await probe_symbol_signal(
            sym,
            stagger_ms=stagger_ms,
            auto_watchlist=auto_watchlist,
        )
        if row.get("error"):
            return row
        market = dict(row.get("market") or {})
        market["symbol"] = sym
        from hunt_core.analysis.pinned_deep import build_pinned_indicator_panel

        tf = row.get("timeframes") or {}
        if tf and not row.get("indicator_panel"):
            row["indicator_panel"] = build_pinned_indicator_panel(sym, tf)
        ms_by_dir: dict[str, Any] = {}
        for direction in ("long", "short"):
            try:
                ms_by_dir[direction] = build_microstructure_context(
                    {**market, "direction": direction}
                )
            except Exception as exc:
                LOG.warning(
                    "microstructure_failed | sym=%s dir=%s error=%s", sym, direction, exc
                )
        if ms_by_dir:
            row["microstructure_by_direction"] = ms_by_dir
            pick = resolve_trade_direction(row)[0]
            row["microstructure"] = ms_by_dir.get(pick) or ms_by_dir.get("long")
        row["pinned_verdict"] = build_pinned_verdict(row)
        row["_deep_analysis"] = True
        price = float(row.get("price") or 0)
        if tf and price > 0 and row.get("mtf") is None:
            from hunt_core.confluence.mtf import build_mtf_confluence

            row["mtf"] = build_mtf_confluence(
                sym,
                tf,
                price,
                market=row.get("market"),
                indicator_panel=row.get("indicator_panel"),
                row=row,
            )
        if cache_is_fresh(sym):
            row["_pinned_cache"] = load_pinned_cache(sym)
        return row
    finally:
        if old_full is None:
            os.environ.pop("HUNT_FULL_PREPARE", None)
        else:
            os.environ["HUNT_FULL_PREPARE"] = old_full


async def _tracker_levels_backtest(client: Any, sym: str) -> dict[str, Any] | None:
    """Mini forward backtest: replay latched levels of the active tracker signal
    over closed 5m bars since open; lets /signal audit compare outcome vs tracker."""
    from datetime import UTC, datetime

    state = load_tracker_state()
    for key, sig in (state.get("signals") or {}).items():
        if not key.startswith(f"{sym}:") or sig.get("status") != "active":
            continue
        direction = str(sig.get("direction") or "")
        try:
            opened = datetime.fromisoformat(str(sig.get("opened_at")))
        except ValueError, TypeError:
            return None
        age_min = (datetime.now(UTC) - opened).total_seconds() / 60.0
        limit = min(1000, max(12, int(age_min / 5) + 2))
        try:
            df = await client.fetch_klines_cached(sym, "5m", limit=limit)
        except Exception as exc:
            LOG.warning("tracker_backtest_klines_failed | sym=%s error=%s", sym, exc)
            return None
        if df is None or df.is_empty():
            return None
        df = df.filter(df["open_time"] >= opened)
        if df.is_empty():
            return None
        bars = list(zip(df["high"].to_list(), df["low"].to_list(), df["close"].to_list(), strict=True))
        setup = {
            "entry_zone": [sig.get("entry_lo"), sig.get("entry_hi")],
            "stop_loss": sig.get("stop_loss"),
            "tp1": sig.get("tp1"),
            "tp2": sig.get("tp2"),
        }
        result = backtest_levels_on_bars(bars, setup=setup, direction=direction)
        result["signal_key"] = key
        result["opened_at"] = str(sig.get("opened_at"))
        result["tracker_tp1_hit"] = bool(sig.get("tp1_hit"))
        return result
    return None


async def deliver_signal_probe(
    broadcaster: TelegramBroadcaster,
    symbol: str,
    *,
    stagger_ms: int = _STAGGER_MS,
) -> dict[str, Any]:
    """Run probe and send a brief Telegram reply (two scenarios + short note)."""
    sym = normalize_symbol(symbol)
    if sym in PINNED_SYMBOLS:
        row = await probe_pinned_deep(sym, stagger_ms=max(stagger_ms, 200), auto_watchlist=True)
    else:
        row = await probe_symbol_signal(sym, stagger_ms=stagger_ms, auto_watchlist=True)
        row["_deep_analysis"] = True
        from hunt_core.analysis.deep_signal import build_poc_level_scenarios

        build_poc_level_scenarios(row)
    audit = row.get("_signal_audit") or {}
    if row.get("error"):
        extra = ""
        if audit.get("issues"):
            extra = "\n<i>audit: " + html.escape(", ".join(audit["issues"][:3])) + "</i>"
        await broadcaster.send_html(
            f"⚠️ <b>/signal</b> {html.escape(sym)}\n<code>{html.escape(str(row['error']))}</code>{extra}",
            no_split=True,
        )
        return row

    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    lc = row.get("lifecycle") or {}

    if dump.get("confirmed"):
        show_dir, conf_setup = "short", dump
    elif long_setup.get("confirmed"):
        show_dir, conf_setup = "long", long_setup
    else:
        show_dir, conf_setup = "", {}

    delivery_tier = None
    if show_dir:
        from hunt_core.deliver.dispatch import evaluate_delivery
        from hunt_core.runtime.state import SNIPER_CONFIG

        gate, delivery_tier = evaluate_delivery(
            row,
            direction=show_dir,
            setup=conf_setup,
            lifecycle=lc if isinstance(lc, dict) else None,
            symbol=sym,
            sniper_config=SNIPER_CONFIG,
            refresh_live_price=True,
        )
        if not gate.ok:
            await broadcaster.send_html(
                f"🚫 <b>/signal blocked</b> {html.escape(sym.replace('USDT', '-USDT'))}\n"
                f"<code>{html.escape(gate.code or 'gate')}</code>\n"
                f"<i>{html.escape(gate.message or '')}</i>",
                no_split=True,
            )
            return row
        if delivery_tier is None:
            await broadcaster.send_html(
                f"⏭ <b>/signal stale</b> {html.escape(sym.replace('USDT', '-USDT'))}\n"
                "<i>Цена уже за TP1 или геометрия входа недействительна</i>",
                no_split=True,
            )
            return row

    from hunt_core.deliver.telegram import format_signal_brief_telegram

    brief = format_signal_brief_telegram(
        row,
        confirmed_direction=show_dir or None,
        added_watch=bool(row.get("_watchlist_added")),
        delivery_tier=delivery_tier,
    )
    if brief:
        from hunt_core.analysis.confluence_grid import build_confluence_grid, format_grid_telegram

        grid = build_confluence_grid(row)
        if grid:
            brief = f"{brief}\n\n{format_grid_telegram(grid)}"
        await broadcaster.send_html(brief, no_split=True)
    return row

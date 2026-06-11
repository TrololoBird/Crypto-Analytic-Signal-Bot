"""On-demand symbol analysis for /signal — rate-limited, separate REST client."""

from __future__ import annotations

import asyncio
import html
import importlib.util
import sys
from pathlib import Path
from typing import Any

from engine.domain.config import load_settings
from engine.features.prepare import min_required_bars
from engine.market.data import BinanceFuturesMarketData
from engine.market.rest_impl import BinanceClientImpl
from engine.telegram import TelegramBroadcaster

from hunt_watch.alert_explain import evaluate_alert_gate, evaluate_formation
from hunt_watch.btc_alignment import (
    btc_market_context,
    forming_confirm_gaps,
    resolve_trade_direction,
    scenario_summary,
)
from hunt_watch.signal_audit import append_audit_log, audit_probe_row, backtest_levels_on_bars
from hunt_watch.signal_tracker import load_tracker_state
from hunt_watch.targets import PINNED_SYMBOLS
from hunt_watch.market_regime import active_params
from hunt_watch.param_store import effective_hunt_params
from hunt_watch.watchlist_ops import add_to_watchlist, register_signal_notify

_STAGGER_MS = 150
_PROBE_TIMEOUT_S = 240.0
_WATCH_MOD: Any | None = None


def _watch_module() -> Any:
    global _WATCH_MOD
    if _WATCH_MOD is not None:
        return _WATCH_MOD
    from hunt_watch.bootstrap import bootstrap

    bootstrap()
    watch_path = Path(__file__).resolve().parents[1] / "scripts" / "watch.py"
    spec = importlib.util.spec_from_file_location("hunt_watch_script", watch_path)
    if spec is None or spec.loader is None:
        msg = f"cannot load watch script: {watch_path}"
        raise ImportError(msg)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hunt_watch_script"] = mod
    spec.loader.exec_module(mod)
    _WATCH_MOD = mod
    return mod


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


def format_signal_probe_telegram(row: dict[str, Any], *, added_watch: bool) -> str:
    """User-facing /signal reply when closed-bar confirm is not yet present."""
    sym = html.escape(str(row.get("symbol", "?")).replace("USDT", "-USDT"))
    if row.get("error"):
        return f"⚠️ <b>/signal {sym}</b>\n<code>{html.escape(str(row['error']))}</code>"

    watch_mod = _watch_module()
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    lc = row.get("lifecycle") or {}
    direction, setup, best_fuel, btc_notes = resolve_direction(row)
    other_dir = "long" if direction == "short" else "short"
    other_setup = long_setup if direction == "short" else dump
    other_fuel = float(
        (other_setup.get("long_fuel") if other_dir == "long" else other_setup.get("dump_fuel"))
        or 0
    )

    short_ok = bool(dump.get("confirmed"))
    long_ok = bool(long_setup.get("confirmed"))
    if short_ok or long_ok:
        return ""  # caller uses full _format_telegram

    price = float(row.get("price") or 0)
    tf = row.get("timeframes") or {}
    pos = row.get("market") or row.get("positioning") or {}
    badge = "🔴" if direction == "short" else "🟢"
    dir_label = "SHORT" if direction == "short" else "LONG"
    bias = str(lc.get("recommended_bias") or "both")
    watch_bias = bias if bias in {"short", "long", "both"} else "both"
    btc_ctx = row.get("btc_context") or {}
    regime = row.get("regime") or {}
    corr = regime.get("btc_corr_1h")

    lines = [
        f"🔍 <b>/signal {sym}</b> · приоритет: {badge} <b>{dir_label}</b>",
        scenario_summary(
            direction=direction,
            setup=setup,
            fuel=best_fuel,
            lc=lc,
            confirmed=False,
        ),
        (
            f"Статус: <code>ждём closed-bar confirm</code> · fuel "
            f"<code>{best_fuel:.0f}</code> vs {other_dir} <code>{other_fuel:.0f}</code>"
        ),
        (
            f"Цена <code>{watch_mod._fmt_price(price)}</code> · 24h "
            f"<code>{row.get('chg_24h_pct')}%</code> · lifecycle "
            f"<code>{html.escape(str(lc.get('phase') or '—'))}</code>"
        ),
        (
            f"<i>Hunt анализирует pump/dump по REST+WS; confirm = закрытый 5m/1m бар. "
            f"Ниже — уровни, OI, funding и триггеры для ручного решения.</i>"
        ),
    ]
    lines.extend(
        watch_mod._format_setup_lines(
            row,
            setup,
            direction=direction,
            tf=tf,
            pos=pos,
            price=price,
        )
    )
    if regime:
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
    gaps = forming_confirm_gaps(setup, direction=direction, tf=tf)
    if gaps:
        lines.append(
            "До confirm нужно: "
            f"<code>{html.escape(', '.join(gaps[:6]))}</code>"
        )
    form = evaluate_formation(setup, direction=direction, symbol=str(row.get("symbol") or ""), lifecycle=lc)
    lines.append(f"Формирование: <i>{html.escape(form.message)}</i>")
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
            f"ℹ️ Уже в watchlist — уведомлю при confirm (<code>dump_confirmed</code> / "
            f"<code>long_confirmed</code>)."
        )
    lines.append("<i>On-demand probe · staggered REST · не прерывает hunt loop</i>")
    return "\n".join(lines)


async def probe_symbol_signal(
    symbol: str,
    *,
    stagger_ms: int = _STAGGER_MS,
    auto_watchlist: bool = True,
) -> dict[str, Any]:
    """Full hunt analysis for one symbol using an isolated REST client."""
    sym = normalize_symbol(symbol)
    if not sym:
        return {"symbol": symbol, "error": "empty_symbol"}

    watch_mod = _watch_module()
    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    client = BinanceFuturesMarketData(
        binance_client=BinanceClientImpl(
            rest_timeout_seconds=45.0,
            futures_data_request_limit_per_5m=max(
                60, settings.runtime.futures_data_request_limit_per_5m // 2
            ),
            proxy_url=settings.network.proxy_url,
            trust_env=settings.network.trust_env,
        ),
    )
    try:
        premium_all = await watch_mod._safe_fetch(client.fetch_premium_index_all()) or {}
        await asyncio.sleep(stagger_ms / 1000.0)
        funding_info_all = await watch_mod._safe_fetch(client.fetch_funding_info_all()) or {}
        await asyncio.sleep(stagger_ms / 1000.0)
        exchange_list = await watch_mod._safe_fetch(client.fetch_exchange_symbols()) or []
        exchange_by_sym = {r.symbol: r for r in exchange_list}
        await asyncio.sleep(stagger_ms / 1000.0)
        ticker_raw = await watch_mod._safe_fetch(client.fetch_ticker_24h()) or []
        ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}

        btc_work_1h = None
        btc_df = await watch_mod._safe_fetch(
            client.fetch_klines_cached("BTCUSDT", "1h", limit=500)
        )
        if btc_df is not None and not btc_df.is_empty():
            btc_work_1h = watch_mod._prepare_frame(btc_df)

        row = await asyncio.wait_for(
            watch_mod._snapshot_symbol(
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
        audit = audit_probe_row(row, source="signal_cmd")
        bt = await _tracker_levels_backtest(client, sym)
        if bt:
            audit["tracker_backtest"] = bt
        append_audit_log(audit)
        row["_signal_audit"] = audit
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
            register_signal_notify(
                sym,
                direction=direction,
                phase="dump_confirmed" if direction == "short" else "long_confirmed",
            )
            row["_watchlist_added"] = added
        return row
    finally:
        await client.close()


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
        except Exception:
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
    """Run probe and send Telegram: confirmed signal or watchlist offer."""
    watch_mod = _watch_module()
    sym = normalize_symbol(symbol)
    await broadcaster.send_html(
        f"⏳ <b>/signal {html.escape(sym.replace('USDT', '-USDT'))}</b> — загружаю историю…"
    )
    row = await probe_symbol_signal(sym, stagger_ms=stagger_ms, auto_watchlist=True)
    audit = row.get("_signal_audit") or {}
    if row.get("error"):
        extra = ""
        if audit.get("issues"):
            extra = "\n<i>audit: " + html.escape(", ".join(audit["issues"][:3])) + "</i>"
        await broadcaster.send_html(
            f"⚠️ <b>/signal</b> {html.escape(sym)}\n<code>{html.escape(str(row['error']))}</code>{extra}"
        )
        return row

    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    direction, setup, fuel, btc_notes = resolve_direction(row)
    lc = row.get("lifecycle") or {}

    if dump.get("confirmed"):
        show_dir, conf_setup = "short", dump
    elif long_setup.get("confirmed"):
        show_dir, conf_setup = "long", long_setup
    else:
        show_dir, conf_setup = "", {}

    if show_dir:
        msg = watch_mod._format_telegram(
            row,
            direction=show_dir,
            confirm_reasons=list(conf_setup.get("confirm_hard") or []),
        )
        show_fuel = float(
            conf_setup.get("dump_fuel") or conf_setup.get("long_fuel") or fuel
        )
        header = scenario_summary(
            direction=show_dir,
            setup=conf_setup,
            fuel=show_fuel,
            lc=lc,
            confirmed=True,
        )
        extras: list[str] = [header]
        btc_ctx = row.get("btc_context") or {}
        regime = row.get("regime") or {}
        corr = regime.get("btc_corr_1h")
        if btc_ctx or corr is not None:
            extras.append(
                f"BTC 1h <code>{btc_ctx.get('btc_chg_1h_pct', '—')}%</code> "
                f"corr <code>{corr if corr is not None else '—'}</code>"
            )
        for note in btc_notes:
            extras.append(f"<i>{html.escape(note)}</i>")
        await broadcaster.send_html("\n".join(extras) + "\n\n" + msg)
        return row

    msg = format_signal_probe_telegram(row, added_watch=bool(row.get("_watchlist_added")))
    if msg:
        audit_line = ""
        if audit.get("ok"):
            audit_line = "\n<i>✓ indie audit OK</i>"
        elif audit.get("issues"):
            audit_line = "\n<i>audit: " + html.escape(", ".join(audit["issues"][:2])) + "</i>"
        await broadcaster.send_html(msg + audit_line)
    return row

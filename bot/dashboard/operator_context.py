"""Operator-facing market context, health, audit, and runtime control summaries."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.runtime.errors import DEFENSIVE_EXC

from .live_audit import audit_snapshot, build_dashboard_audit_snapshot

if TYPE_CHECKING:
    from bot.runtime.bot import SignalBot

    from .live import DashboardLiveData


def format_runtime_ops_block(
    *,
    tag: str | None = None,
    runtime_policy: dict[str, Any] | None = None,
    readiness: dict[str, Any] | None = None,
    ws_health: dict[str, Any] | None = None,
    frame_readiness: dict[str, Any] | None = None,
    latest_cycle: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> str:
    """WS / policy / frames footer appended to market context messages."""

    def clean(value: object, fallback: str) -> str:
        raw = str(value or "").strip()
        if not raw or raw.lower() in {"unknown", "n/a", "none"} or raw.startswith("disabled_"):
            return fallback
        return raw

    runtime_policy = runtime_policy or {}
    readiness = readiness or {}
    ws_health = ws_health or {}
    frame_readiness = frame_readiness or {}
    latest_cycle = latest_cycle or {}
    metrics = metrics or {}

    runtime_mode = html.escape(clean(runtime_policy.get("runtime_mode"), "signal_only"))
    source_policy = html.escape(clean(runtime_policy.get("source_policy"), "binance_only"))
    pause_losses = html.escape(clean(runtime_policy.get("max_consecutive_stop_losses"), "3"))
    pause_hours = html.escape(clean(runtime_policy.get("stop_loss_pause_hours"), "5"))
    shortlist_source = html.escape(clean(readiness.get("shortlist_source"), "rest_full"))
    shortlist_size = html.escape(clean(readiness.get("shortlist_size"), "0"))
    latest_ts = html.escape(clean(latest_cycle.get("ts"), "-"))

    title = "⚙️ <b>Runtime</b>"
    if tag:
        title = f"{title} <code>{html.escape(tag)}</code>"

    lines = [
        title,
        (
            f"Policy: runtime=<code>{runtime_mode}</code> "
            f"source=<code>{source_policy}</code> "
            f"pause=<code>{pause_losses}/{pause_hours}h</code>"
        ),
        (f"Shortlist: source=<code>{shortlist_source}</code> size=<code>{shortlist_size}</code>"),
        (
            "WS: streams="
            f"<code>{html.escape(clean(ws_health.get('active_stream_count'), '0'))}</code> "
            "reconnect="
            f"<code>{html.escape(clean(ws_health.get('reconnect_reason'), 'steady'))}</code>"
        ),
        (
            "Frames ready: 15m="
            f"<code>{frame_readiness.get('15m_ready_symbols', 0)}</code> "
            f"1h=<code>{frame_readiness.get('1h_ready_symbols', 0)}</code> "
            f"4h=<code>{frame_readiness.get('4h_ready_symbols', 0)}</code>"
        ),
    ]
    if metrics:
        lines.append(
            f"Tracked: open <code>{metrics.get('open_tracked_total', 0)}</code> | "
            f"outcomes <code>{metrics.get('outcomes_total', 0)}</code>"
        )
    if latest_ts != "-":
        lines.append(f"Latest cycle: <code>{latest_ts}</code>")
    return "\n".join(lines)


def format_market_from_display_snapshot(snapshot: dict[str, Any]) -> str:
    """Rebuild rich Telegram HTML from persisted display snapshot."""
    if not snapshot:
        return ""

    risk_label = html.escape(str(snapshot.get("risk_label") or "neutral"))
    fear_value = int(snapshot.get("fear_greed_value") or 50)
    fear_label = html.escape(str(snapshot.get("fear_greed_label") or "Neutral"))
    practical = html.escape(str(snapshot.get("practical") or ""))
    positive = int(snapshot.get("breadth_positive") or 0)
    total = int(snapshot.get("breadth_total") or 0)
    breadth_pct = float(snapshot.get("breadth_pct") or 0.0)

    def pct(value: object, *, signed: bool = True, digits: int = 1) -> str:
        try:
            numeric = float(value)
        except TypeError, ValueError:
            numeric = 0.0
        if signed:
            return f"{numeric:+.{digits}f}%"
        return f"{numeric:.{digits}f}%"

    lines = [
        "🧭 <b>Контекст рынка</b>",
        (
            f"Итог: <code>{risk_label}</code>; "
            f"fear/greed proxy <code>{fear_value} ({fear_label})</code>"
        ),
        f"Практически: {practical}.",
        (
            "Ширина рынка: "
            f"<code>{positive}</code> из <code>{total}</code> ликвидных в плюсе "
            f"(<code>{pct(breadth_pct, signed=False, digits=0)}</code>)"
        ),
        html.escape(str(snapshot.get("tf_4h") or "4h: n/a")),
        html.escape(str(snapshot.get("tf_1h") or "1h: n/a")),
        html.escape(str(snapshot.get("tf_15m") or "15m: n/a")),
        *(
            [html.escape(str(snapshot.get("intraday_note")))]
            if snapshot.get("intraday_note")
            else []
        ),
        (
            "Крипто-драйверы: "
            f"BTC <code>{pct(snapshot.get('btc_24h_pct'))}</code> | "
            f"ETH <code>{pct(snapshot.get('eth_24h_pct'))}</code> | "
            f"SOL <code>{pct(snapshot.get('sol_24h_pct'))}</code>"
        ),
        (
            "Доминация фьючерсного объема: "
            f"BTC <code>{pct(snapshot.get('volume_btc_pct'), signed=False)}</code> | "
            f"ETH <code>{pct(snapshot.get('volume_eth_pct'), signed=False)}</code> | "
            f"SOL <code>{pct(snapshot.get('volume_sol_pct'), signed=False)}</code> | "
            f"альты <code>{pct(snapshot.get('volume_alts_pct'), signed=False)}</code> | "
            f"стейбл-базы <code>{pct(snapshot.get('volume_stables_pct'), signed=False)}</code>"
        ),
        f"Макро-прокси: <code>{html.escape(str(snapshot.get('macro_line') or ''))}</code>",
        html.escape(str(snapshot.get("corr_line") or "")),
        html.escape(str(snapshot.get("corr_narrative") or "")),
        (
            "Лидеры 24ч (liquid futures): "
            f"<code>{html.escape(str(snapshot.get('leaders') or '-'))}</code>"
        ),
        (
            "Аутсайдеры 24ч (liquid futures): "
            f"<code>{html.escape(str(snapshot.get('laggards') or '-'))}</code>"
        ),
        (
            "Сопровождение: active "
            f"<code>{int(snapshot.get('tracking_active') or 0)}</code> | "
            f"pending <code>{int(snapshot.get('tracking_pending') or 0)}</code>"
        ),
    ]
    updated = snapshot.get("updated_at")
    if updated:
        lines.append(f"Обновлено: <code>{html.escape(str(updated))}</code>")
    return "\n".join(line for line in lines if line)


def format_market_context_warmup(
    *, note: str = "полный контекст через 1-2 мин после прогрева REST/WS"
) -> str:
    return (
        "🧭 <b>Контекст рынка</b> <code>warmup</code>\n"
        f"<i>{html.escape(note)}</i>\n"
        "Используйте /market после прогрева shortlist и tickers."
    )


async def resolve_operator_market_html(
    bot: SignalBot,
    *,
    tag: str | None = None,
    include_runtime: bool = True,
) -> str:
    """Best available rich market context for operator Telegram or startup."""
    html_body = ""
    updater = getattr(bot, "_market_context_updater", None)
    if updater is not None:
        html_body = str(getattr(updater, "_last_market_state_html", "") or "").strip()
        if not html_body:
            try:
                html_body = await updater.build_market_state_html(force=False)
            except DEFENSIVE_EXC:
                html_body = ""

    if not html_body:
        repo = getattr(bot, "_modern_repo", None)
        if repo is not None:
            try:
                ctx = await repo.get_market_context()
            except DEFENSIVE_EXC:
                ctx = {}
            html_body = str(ctx.get("telegram_html") or "").strip()
            if not html_body:
                display = ctx.get("display_snapshot")
                if isinstance(display, dict):
                    html_body = format_market_from_display_snapshot(display)

    if not html_body:
        html_body = format_market_context_warmup()

    if tag and html_body.startswith("🧭"):
        html_body = html_body.replace(
            "🧭 <b>Контекст рынка</b>",
            f"🧭 <b>Контекст рынка</b> <code>{html.escape(tag)}</code>",
            1,
        )

    if not include_runtime:
        return html_body

    runtime_block = await format_operator_runtime_block(bot)
    return f"{html_body}\n\n{runtime_block}"


async def format_operator_runtime_block(bot: SignalBot) -> str:
    live_data = getattr(getattr(bot, "dashboard", None), "_live_data", None)
    overview: dict[str, Any] = {}
    if live_data is not None:
        try:
            overview = live_data.overview() or {}
        except DEFENSIVE_EXC:
            overview = {}

    settings = bot.settings
    intelligence = getattr(settings, "intelligence", None)
    runtime_policy = {
        "runtime_mode": getattr(intelligence, "runtime_mode", "signal_only"),
        "source_policy": getattr(intelligence, "source_policy", "binance_only"),
        "max_consecutive_stop_losses": getattr(intelligence, "max_consecutive_stop_losses", 3),
        "stop_loss_pause_hours": getattr(intelligence, "stop_loss_pause_hours", 5),
    }
    ws = getattr(bot, "_ws_manager", None)
    ws_snapshot: dict[str, Any] = {}
    if ws is not None and hasattr(ws, "state_snapshot"):
        raw = ws.state_snapshot()
        if isinstance(raw, dict):
            ws_snapshot = raw

    shortlist = list(
        getattr(bot, "_last_live_shortlist", []) or getattr(bot, "_shortlist", []) or []
    )
    return format_runtime_ops_block(
        runtime_policy=runtime_policy,
        readiness={
            "shortlist_source": getattr(bot, "_shortlist_source", "unknown"),
            "shortlist_size": len(shortlist),
        },
        ws_health={
            "active_stream_count": ws_snapshot.get("active_streams")
            or ws_snapshot.get("stream_count")
            or overview.get("active_stream_count"),
            "reconnect_reason": ws_snapshot.get("reconnect_reason") or "steady",
        },
        frame_readiness={
            "15m_ready_symbols": overview.get("frames_15m_ready"),
            "1h_ready_symbols": overview.get("frames_1h_ready"),
            "4h_ready_symbols": overview.get("frames_4h_ready"),
        },
        latest_cycle=overview.get("last_cycle")
        if isinstance(overview.get("last_cycle"), dict)
        else {},
    )


async def format_operator_health_text(bot: SignalBot) -> str:
    health_mgr = getattr(bot, "_health_manager", None)
    if health_mgr is None:
        return "<b>Health</b>\nНедоступно."
    try:
        health = await health_mgr.health_check()
    except DEFENSIVE_EXC:
        return "<b>Health</b>\nОшибка чтения."

    ws = getattr(bot, "_ws_manager", None)
    ws_snap: dict[str, Any] = {}
    if ws is not None and hasattr(ws, "state_snapshot"):
        raw = ws.state_snapshot()
        if isinstance(raw, dict):
            ws_snap = raw

    client = getattr(bot, "client", None)
    rest_pause = ""
    if client is not None:
        snap_fn = getattr(client, "state_snapshot", None)
        snap = snap_fn() if callable(snap_fn) else {}
        if not isinstance(snap, dict):
            snap = {}
        pause_remaining = max(
            float(snap.get("rest_rate_limit_pause_remaining_s") or 0.0),
            float(snap.get("futures_data_pause_remaining_s") or 0.0),
            float(snap.get("funding_endpoint_pause_remaining_s") or 0.0),
        )
        if pause_remaining > 0:
            rest_pause = f"REST pause active ({pause_remaining:.0f}s remaining)"

    lines = [
        "<b>Health</b>",
        f"Status: <code>{html.escape(str(health.get('status') or 'unknown'))}</code>",
        f"WS: <code>{'connected' if health.get('ws_connected') else 'down'}</code> · "
        f"shortlist <code>{health.get('shortlist_size') or 0}</code>",
        (
            f"Signals pending/active: <code>{health.get('active_signals') or 0}</code> · "
            f"pending outcomes <code>{health.get('pending_outcomes') or 0}</code>"
        ),
        (
            f"Fresh tickers <code>{health.get('fresh_tickers') or 0}</code> · "
            f"stale klines <code>{health.get('stale_kline_streams') or 0}</code> · "
            "last kline age "
            f"<code>{round(float(health.get('last_kline_event_age_seconds') or 0), 1)}s</code>"
        ),
    ]
    radar = health.get("radar") if isinstance(health.get("radar"), dict) else {}
    if radar.get("enabled"):
        lines.append(
            "Radar: "
            f"status=<code>{html.escape(str(radar.get('status') or 'unknown'))}</code> "
            f"symbols=<code>{radar.get('symbol_count') or 0}</code> "
            f"tiers=<code>{html.escape(str(radar.get('tiers') or {}))}</code>"
        )
    if ws_snap:
        ws_lag = ws_snap.get("latency_ms") or ws_snap.get("current_latency_ms") or "-"
        ws_streams = ws_snap.get("active_streams") or ws_snap.get("stream_count") or "-"
        lines.append(f"WS lag <code>{ws_lag}ms</code> · streams <code>{ws_streams}</code>")
    if rest_pause:
        lines.append(html.escape(rest_pause))
    lines.append(f"<i>{datetime.now(UTC).strftime('%H:%M:%S UTC')}</i>")
    return "\n".join(lines)


async def format_operator_audit_text(_bot: SignalBot, live_data: DashboardLiveData | None) -> str:
    if live_data is None:
        return "<b>Audit</b>\nLive data недоступна."

    def _build() -> dict[str, Any]:
        snapshot = build_dashboard_audit_snapshot(
            overview=live_data.overview(),
            funnel=live_data.funnel(max_rows=20_000),
            shortlist=live_data.shortlist(),
            decisions=live_data.decisions(max_rows=20_000),
            rejections=live_data.rejections(max_rows=20_000),
            delivery=live_data.delivery(),
            runtime=live_data.runtime(),
            telegram=live_data.telegram_preview(),
        )
        return audit_snapshot(snapshot)

    try:
        audit = _build()
    except DEFENSIVE_EXC:
        return "<b>Audit</b>\nОшибка построения."

    summary = audit.get("summary") or {}
    lines = [
        "<b>Live audit</b>",
        f"Score: <code>{audit.get('score') or 0}/100</code> · "
        f"status <code>{html.escape(str(audit.get('status') or 'unknown'))}</code>",
        (
            f"Findings: critical <code>{summary.get('critical') or 0}</code> · "
            f"warning <code>{summary.get('warning') or 0}</code> · "
            f"info <code>{summary.get('info') or 0}</code>"
        ),
        html.escape(str(audit.get("operator_brief") or "")),
    ]
    plan = audit.get("action_plan") or []
    if plan:
        lines.append("<b>Action</b>")
        lines.extend(f"• {html.escape(str(item))}" for item in plan[:3])
    return "\n".join(lines)


def format_operator_policy_text(bot: SignalBot) -> str:
    settings = bot.settings
    intel = getattr(settings, "intelligence", None)
    universe = getattr(settings, "universe", None)
    notifiers = getattr(settings, "notifiers", None)
    op = getattr(notifiers, "telegram_operator", None)

    lines = [
        "<b>Policy / routing</b>",
        "<b>Канал</b> - только сигналы и tracking для подписчиков.",
        "<b>Личка оператора</b> - /market, digest, SL analytics, startup.",
        f"Runtime: <code>{getattr(intel, 'runtime_mode', 'signal_only')}</code> · "
        f"source <code>{getattr(intel, 'source_policy', 'binance_only')}</code>",
        (
            f"SL pause: <code>{getattr(intel, 'max_consecutive_stop_losses', 3)}</code> losses / "
            f"<code>{getattr(intel, 'stop_loss_pause_hours', 5)}h</code>"
        ),
        "Shortlist unified: "
        f"<code>{getattr(universe, 'shortlist_unified_routing', False)}</code> · "
        f"size target <code>{getattr(universe, 'shortlist_size', '-')}</code>",
        f"Notifier channel: <code>{getattr(notifiers, 'provider', 'telegram')}</code>",
        f"Operator DMs: market=<code>{getattr(op, 'send_market_context', True)}</code> "
        f"digest=<code>{getattr(op, 'send_digest', True)}</code> "
        f"SL=<code>{getattr(op, 'send_sl_postmortem', True)}</code>",
        "Operator IDs: "
        f"<code>{len(getattr(settings, 'operator_user_ids', ()) or ())}</code> configured",
        "<i>Read-only · правки через config.toml</i>",
    ]
    return "\n".join(lines)


def format_operator_cycles_text(
    payload: dict[str, Any], overview: dict[str, Any] | None = None
) -> str:
    overview = overview or {}
    today = payload.get("today") or {}
    last_cycle = overview.get("last_cycle") if isinstance(overview.get("last_cycle"), dict) else {}
    selected_count = last_cycle.get("selected_count") or last_cycle.get("selected_signals") or 0
    lines = [
        "<b>Pipeline cycles</b>",
        f"Last cycle: <code>{html.escape(str(last_cycle.get('ts') or '-'))}</code>",
        (
            f"Candidates <code>{last_cycle.get('candidate_count') or 0}</code> · "
            f"selected <code>{selected_count}</code> · "
            f"delivered <code>{last_cycle.get('delivered_count') or 0}</code>"
        ),
        (
            f"Today sent <code>{today.get('signals_sent') or 0}</code> · "
            f"session delivered <code>{today.get('session_delivered') or 0}</code> · "
            f"rejections logged <code>{today.get('rejections') or 0}</code>"
        ),
    ]
    top = overview.get("top_rejection") if isinstance(overview.get("top_rejection"), dict) else {}
    top_reason = top.get("key") or top.get("reason")
    if top_reason:
        lines.append(
            f"Top reject: <code>{html.escape(str(top_reason))}</code> "
            f"x <code>{top.get('count') or 0}</code>"
        )
    return "\n".join(lines)

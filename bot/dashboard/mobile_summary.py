"""Compact dashboard payload for mobile browsers and Telegram digests."""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .outcomes_insights import build_outcomes_insights
from .user_summary import build_user_summary

if TYPE_CHECKING:
    from .live import DashboardLiveData


def _lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return None


def dashboard_urls(bot: Any) -> dict[str, str | None]:
    settings = getattr(bot, "settings", None)
    host = str(getattr(getattr(settings, "runtime", None), "dashboard_host", "127.0.0.1") or "127.0.0.1")
    port = int(getattr(getattr(settings, "runtime", None), "dashboard_port", 8080) or 8080)
    lan = _lan_ip()
    local = f"http://127.0.0.1:{port}/"
    lan_url = f"http://{lan}:{port}/" if lan else None
    bind_url = f"http://{host}:{port}/" if host not in ("127.0.0.1", "localhost") else local
    return {
        "local": local,
        "lan": lan_url,
        "bind": bind_url,
        "mobile_hint": (
            "MacBook и iPhone в разных сетях: LAN недоступен. "
            "Удалённый мониторинг — Telegram operator console: /market /status /health в личке с ботом. "
            "Локально на MacBook: http://127.0.0.1:{port}/"
        ).format(port=port),
        "remote_access": {
            "mode": "local_only",
            "env": "TELEGRAM_OPERATOR_USER_IDS",
            "commands": [
                "/market",
                "/status",
                "/health",
                "/audit",
                "/sl",
                "/tracking",
                "/open",
                "/pending",
                "/active",
                "/delivery",
                "/shortlist",
                "/rejections",
                "/gates",
                "/strategies",
                "/cycles",
                "/notify",
                "/policy",
                "/symbol",
                "/signal",
                "/analyze",
                "/scan",
                "/review",
                "/refresh",
                "/help",
            ],
        },
    }


async def build_mobile_summary(bot: Any, live_data: DashboardLiveData) -> dict[str, Any]:
    """Single payload for /api/v1/mobile/summary and Telegram digest."""
    summary = await build_user_summary(bot, live_data)
    repo = getattr(bot, "_modern_repo", None)
    outcomes = (
        await build_outcomes_insights(repo, days=7)
        if repo is not None
        else {"summary": {}, "patterns": [], "recommendations": []}
    )
    regime = getattr(bot, "market_regime", None)
    last_regime = getattr(regime, "_last_result", None) if regime is not None else None
    updater = getattr(bot, "_market_context_updater", None)
    market_display = getattr(updater, "_last_display_snapshot", None) if updater else None
    ws = getattr(bot, "_ws_manager", None)
    shortlist = list(getattr(bot, "_last_live_shortlist", []) or [])
    urls = dashboard_urls(bot)
    sl_causes = outcomes.get("sl_root_causes") or {}
    urls["remote_access"] = {
        "mode": "telegram_operator" if getattr(bot.settings, "operator_user_ids", ()) else "local_only",
        "env": "TELEGRAM_OPERATOR_USER_IDS",
        "commands": [
            "/market",
            "/status",
            "/health",
            "/audit",
            "/sl",
            "/tracking",
            "/shortlist",
            "/rejections",
            "/cycles",
            "/policy",
            "/help",
        ],
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "dashboard_urls": urls,
        "runtime": {
            "shortlist_size": len(shortlist),
            "ws_connected": bool(ws and getattr(ws, "is_connected", lambda: False)()),
            "regime": getattr(last_regime, "regime", None) if last_regime else None,
            "btc_bias": getattr(last_regime, "btc_bias", None) if last_regime else None,
        },
        "market_display": market_display or {},
        "today": summary.get("today") or {},
        "outcomes_7d": outcomes.get("summary") or {},
        "sl_root_causes": sl_causes,
        "sl_root_cause_labels": outcomes.get("sl_root_cause_labels") or {},
        "top_sl_patterns": (outcomes.get("patterns") or [])[:4],
        "recommendations": (outcomes.get("recommendations") or [])[:3],
        "recent_stop_losses": (outcomes.get("recent_stop_losses") or [])[:5],
        "rejections_hint": "/api/live/rejections · вкладка Funnel в dashboard",
    }


def format_mobile_digest_text(payload: dict[str, Any]) -> str:
    """Plain-text digest safe for Telegram HTML escape layer."""
    urls = payload.get("dashboard_urls") or {}
    runtime = payload.get("runtime") or {}
    today = payload.get("today") or {}
    outcomes = payload.get("outcomes_7d") or {}
    market = payload.get("market_display") or {}
    lines = [
        "<b>Bot digest</b>",
    ]
    if market.get("risk_label"):
        lines.append(
            f"Market: <code>{market.get('risk_label')}</code> · "
            f"FG <code>{market.get('fear_greed_value') or '—'}</code> · "
            f"breadth <code>{market.get('breadth_positive') or 0}/{market.get('breadth_total') or 0}</code>"
        )
    lines.extend([
        f"Regime: <code>{runtime.get('regime') or '—'}</code> · BTC: <code>{runtime.get('btc_bias') or '—'}</code>",
        f"Shortlist: <code>{runtime.get('shortlist_size') or 0}</code> · WS: <code>{'ok' if runtime.get('ws_connected') else 'down'}</code>",
        f"Today signals: <code>{today.get('signals_sent') or 0}</code> · delivered: <code>{today.get('session_delivered') or 0}</code>",
        (
            f"7d outcomes: <code>{outcomes.get('wins') or 0}W</code> / "
            f"<code>{outcomes.get('stop_losses') or 0}SL</code> · "
            f"WR <code>{round(float(outcomes.get('win_rate') or 0) * 100, 1)}%</code>"
        ),
    ])
    causes = payload.get("sl_root_causes") or {}
    if causes:
        top = sorted(causes.items(), key=lambda item: -int(item[1]))[:3]
        cause_line = ", ".join(f"{key}={count}" for key, count in top)
        lines.append(f"SL causes: <code>{cause_line}</code>")
    recs = payload.get("recommendations") or []
    if recs:
        lines.append(f"Tip: {recs[0][:180]}")
    remote = (payload.get("dashboard_urls") or {}).get("remote_access") or {}
    if remote.get("mode") == "telegram_operator":
        cmds = " · ".join(str(c) for c in (remote.get("commands") or [])[:4])
        lines.append(f"Remote: напишите боту <code>{cmds}</code> … /help")
    else:
        lan = urls.get("lan") or urls.get("local")
        if lan:
            lines.append(f'Dashboard (LAN): <a href="{lan}">{lan}</a>')
    lines.append("<i>Signal-only · no auto-trading</i>")
    return "\n".join(lines)


def format_operator_help_text() -> str:
    return (
        "<b>Operator console</b> (личка с ботом)\n"
        "Отдельно от канала с сигналами — ops только здесь.\n\n"
        "<b>📢 Канал (TARGET_CHAT_ID)</b>\n"
        "Только ACTION/WATCH сигналы и статусы сделок для подписчиков.\n"
        "Контекст рынка, audit, digest, startup — <u>не</u> в канал.\n\n"
        "<b>🧭 Рынок (личка)</b>\n"
        "/market — полный контекст (breadth, TF, leaders, corr)\n"
        "/status — режим, shortlist, сегодня, 7d outcomes\n"
        "/health — WS, REST, klines, signals\n"
        "/audit — live audit score + action plan\n\n"
        "<b>📊 Сигналы (личка)</b>\n"
        "/signals — сегодня sent/delivered\n"
        "/sl — причины stop-loss + последние SL\n"
        "/tracking · /open — pending/active + entry/SL\n"
        "/pending · /active — фильтр статуса\n"
        "/delivery — канал + chase/TTL\n"
        "/outcomes — детальная статистика 7d\n\n"
        "<b>⚙️ Pipeline (личка)</b>\n"
        "/shortlist — режим + топ символов\n"
        "/rejections · /gates — воронка\n"
        "/strategies — enabled + MTF\n"
        "/cycles — последний цикл + funnel\n"
        "/digest — авто-digest (~30 мин)\n"
        "/notify — флаги operator DM\n\n"
        "<b>🔧 Config (личка, read-only)</b>\n"
        "/policy — runtime policy\n"
        "/help — этот список\n\n"
        "<b>🎛 Управление ботом</b>\n"
        "/symbol BTC — найти сигналы по монете + rejections\n"
        "/signal REF — аналитика сигнала (ref или символ)\n"
        "/analyze BTC — принудительный прогон 38 стратегий\n"
        "/scan — полный скан shortlist (emergency cycle)\n"
        "/scan BTC — analyze одной монеты\n"
        "/review BTC — принудительный tracking review\n"
        "/refresh shortlist — обновить shortlist + WS\n"
        "/refresh market — market context + regime\n"
        "/shortlist refresh — то же что refresh shortlist\n"
        "/market refresh — обновить контекст рынка\n\n"
        "<i>Signal-only · без auto-trade · одна action-команда за раз</i>"
    )


def format_operator_status_text(payload: dict[str, Any], *, detail_outcomes: bool = False) -> str:
    runtime = payload.get("runtime") or {}
    today = payload.get("today") or {}
    outcomes = payload.get("outcomes_7d") or {}
    lines = [
        "<b>Bot status</b>",
        f"Regime: <code>{runtime.get('regime') or '—'}</code> · BTC: <code>{runtime.get('btc_bias') or '—'}</code>",
        f"Shortlist: <code>{runtime.get('shortlist_size') or 0}</code> · WS: <code>{'ok' if runtime.get('ws_connected') else 'down'}</code>",
        f"Today: sent <code>{today.get('signals_sent') or 0}</code> · session delivered <code>{today.get('session_delivered') or 0}</code>",
        f"Tracking pending/active: <code>{today.get('pending') or 0}</code>/<code>{today.get('active') or 0}</code>",
        (
            f"7d: <code>{outcomes.get('wins') or 0}W</code> / "
            f"<code>{outcomes.get('stop_losses') or 0}SL</code> · "
            f"WR <code>{round(float(outcomes.get('win_rate') or 0) * 100, 1)}%</code>"
        ),
    ]
    if detail_outcomes:
        patterns = payload.get("top_sl_patterns") or []
        for row in patterns[:3]:
            if isinstance(row, dict):
                lines.append(f"• {row.get('label') or row.get('pattern')}: <code>{row.get('count') or 0}</code>")
    return "\n".join(lines)


def format_operator_sl_text(payload: dict[str, Any]) -> str:
    outcomes = payload.get("outcomes_7d") or {}
    labels = payload.get("sl_root_cause_labels") or {}
    causes = payload.get("sl_root_causes") or {}
    lines = [
        "<b>Stop-loss analytics</b>",
        f"7d SL: <code>{outcomes.get('stop_losses') or 0}</code> · WR <code>{round(float(outcomes.get('win_rate') or 0) * 100, 1)}%</code>",
    ]
    if causes:
        lines.append("<b>Root causes</b>")
        for code, count in sorted(causes.items(), key=lambda item: -int(item[1]))[:8]:
            label = labels.get(code) or code
            lines.append(f"• {label}: <code>{count}</code>")
    recent = payload.get("recent_stop_losses") or []
    if recent:
        lines.append("<b>Recent SL</b>")
        for row in recent[:5]:
            if not isinstance(row, dict):
                continue
            sym = row.get("symbol") or "?"
            setup = row.get("setup_type") or row.get("setup_id") or "?"
            cause = row.get("sl_root_cause_label") or row.get("sl_root_cause") or "—"
            lines.append(f"• {sym} {row.get('direction') or ''} · {setup} · {cause}")
    recs = payload.get("recommendations") or []
    if recs:
        lines.append(f"Tip: {recs[0][:200]}")
    return "\n".join(lines)

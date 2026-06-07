"""Human-friendly dashboard summary - today, funnel hint, recent history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bot.domain.labels import reject_reason_ru, result_label_ru
from bot.runtime.errors import DEFENSIVE_EXC

from .tracking_view import serialize_tracking_signal

if TYPE_CHECKING:
    from .live import DashboardLiveData

JsonDict = dict[str, Any]

# Re-export for backward-compatible imports within dashboard package.
__all__ = ["build_funnel_hint", "build_user_summary", "reject_reason_ru", "result_label_ru"]


def build_funnel_hint(*, overview: JsonDict | None, funnel: JsonDict | None) -> JsonDict:
    overview = overview or {}
    funnel = funnel or {}
    totals = funnel.get("cycle_totals") if isinstance(funnel.get("cycle_totals"), dict) else {}
    candidates = int(
        totals.get("candidate_count")
        or totals.get("candidates")
        or overview.get("last_cycle_candidates")
        or 0
    )
    delivered = int(
        overview.get("session_delivered")
        or totals.get("delivered_count")
        or totals.get("delivered")
        or overview.get("last_cycle_delivered")
        or 0
    )
    top = overview.get("top_blocker") if isinstance(overview.get("top_blocker"), dict) else {}
    if not top:
        top = (
            overview.get("top_rejection") if isinstance(overview.get("top_rejection"), dict) else {}
        )
    combined = (
        funnel.get("combined_reject_hint")
        if isinstance(funnel.get("combined_reject_hint"), dict)
        else {}
    )
    top_key = str(top.get("key") or top.get("reason") or combined.get("key") or "")
    top_count = int(top.get("count") or combined.get("count") or 0)
    top_ru = (
        top.get("label_ru")
        or combined.get("label_ru")
        or (reject_reason_ru(top_key) if top_key else None)
    )

    if delivered > 0:
        text = f"За сессию отправлено {delivered} сигнал(ов)."
        if candidates:
            text += f" Из {candidates} кандидатов прошли фильтры."
    elif candidates > 0:
        text = f"Кандидатов {candidates}, отправлено 0."
        if top_ru and top_count:
            text += f" Чаще всего отсеивает: {top_ru} ({top_count})."
        else:
            text += " Бот работает - сигналы пока не проходят порог доставки."
    else:
        text = "Бот анализирует рынок. Сигналов в telemetry пока нет - подождите 1-2 цикла."

    btc_bias = overview.get("btc_bias")
    if btc_bias:
        bias_map = {"uptrend": "рост", "downtrend": "падение", "neutral": "боковик"}
        text += f" BTC: {bias_map.get(str(btc_bias).lower(), btc_bias)}."

    return {
        "text": text,
        "candidates": candidates,
        "delivered": delivered,
        "top_filter": top_key or None,
        "top_filter_ru": top_ru,
        "top_filter_count": top_count,
    }


async def build_today_summary(bot: Any, live_data: DashboardLiveData | None) -> JsonDict:
    repo = getattr(bot, "_modern_repo", None)
    stats: JsonDict = {
        "signals_sent": 0,
        "activated": 0,
        "tp1_hit": 0,
        "tp2_hit": 0,
        "stop_loss": 0,
        "expired": 0,
        "active": 0,
        "pending": 0,
    }
    if repo is not None:
        try:
            raw = await repo.get_tracking_stats()
            stats.update({k: int(raw.get(k) or 0) for k in stats})
        except DEFENSIVE_EXC:
            pass
        try:
            rows = await repo.get_active_signals(include_closed=False)
            stats["pending"] = sum(1 for r in rows if r.get("status") == "pending")
            stats["active"] = sum(1 for r in rows if r.get("status") == "active")
        except DEFENSIVE_EXC:
            pass

    overview = live_data.overview() if live_data is not None else {}
    funnel = live_data.funnel() if live_data is not None else {}
    hint = build_funnel_hint(overview=overview, funnel=funnel)
    session_delivered = int(hint.get("delivered") or 0)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "signals_sent": int(stats.get("signals_sent") or 0),
        "activated": int(stats.get("activated") or 0),
        "tp1_hit": int(stats.get("tp1_hit") or 0),
        "tp2_hit": int(stats.get("tp2_hit") or 0),
        "stop_loss": int(stats.get("stop_loss") or 0),
        "expired": int(stats.get("expired") or 0),
        "pending": int(stats.get("pending") or 0),
        "active": int(stats.get("active") or 0),
        "session_delivered": session_delivered,
        "funnel_hint": hint,
    }


async def build_signal_history(bot: Any, *, days: int = 7, limit: int = 30) -> list[JsonDict]:
    repo = getattr(bot, "_modern_repo", None)
    if repo is None:
        return []

    since = datetime.now(UTC) - timedelta(days=max(1, int(days)))
    history: list[JsonDict] = []

    try:
        outcomes = await repo.get_signal_outcomes(since=since, limit=limit)
        history.extend(
            {
                "symbol": row.get("symbol"),
                "setup_id": row.get("setup_id"),
                "direction": row.get("direction"),
                "result": row.get("result"),
                "result_ru": result_label_ru(str(row.get("result") or "")),
                "pnl_pct": row.get("pnl_pct"),
                "closed_at": row.get("closed_at") or row.get("created_at"),
                "tracking_id": row.get("tracking_id"),
                "source": "outcome",
            }
            for row in outcomes
        )
    except DEFENSIVE_EXC:
        pass

    if len(history) < limit:
        try:
            closed_rows = await repo.get_active_signals(status="closed", include_closed=True)
            seen = {str(h.get("tracking_id")) for h in history if h.get("tracking_id")}
            for row in closed_rows:
                tid = str(row.get("tracking_id") or "")
                if tid and tid in seen:
                    continue
                created = row.get("closed_at") or row.get("created_at")
                if created:
                    try:
                        ts = datetime.fromisoformat(str(created))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=UTC)
                        if ts < since:
                            continue
                    except (TypeError, ValueError):
                        continue
                reason = str(row.get("close_reason") or "closed")
                history.append(
                    {
                        "symbol": row.get("symbol"),
                        "setup_id": row.get("setup_id"),
                        "direction": row.get("direction"),
                        "result": reason,
                        "result_ru": result_label_ru(reason),
                        "pnl_pct": None,
                        "closed_at": created,
                        "tracking_id": tid or None,
                        "source": "active_signals",
                    }
                )
                if len(history) >= limit:
                    break
        except DEFENSIVE_EXC:
            pass

    history.sort(key=lambda item: str(item.get("closed_at") or ""), reverse=True)
    return history[:limit]


async def build_user_summary(bot: Any, live_data: DashboardLiveData | None) -> JsonDict:
    today = await build_today_summary(bot, live_data)
    history = await build_signal_history(bot, days=7, limit=25)
    open_rows: list[JsonDict] = []
    repo = getattr(bot, "_modern_repo", None)
    if repo is not None:
        try:
            active = await repo.get_active_signals(include_closed=False)
            open_rows = [serialize_tracking_signal(row, bot) for row in active[:10]]
        except DEFENSIVE_EXC:
            pass
    return {
        "today": today,
        "history": history,
        "open_signals": open_rows,
        "funnel_hint": today.get("funnel_hint") or {},
    }

"""Query plane — read materialized watch state and explain (QueryResult ≠ DeliveryGate).

TG ``/signal`` and ``_dev/probe_delivery`` use this module. Delivery still runs
``evaluate_delivery*`` only to answer *would deliver now*; blockers are always listed.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.scanner.detect.delivery_support import GateResult
from hunt_core.market.client import HuntCcxtClient

STORE_FRESH_S = 180.0
STORE_STALE_S = 600.0
_HOT_TICK_PATHS = frozenset({"hot_ws", "hot_bootstrap", "hot_delta", "hot_carry"})
_MAX_BLOCKERS_SHOWN = 5


@dataclass(frozen=True, slots=True)
class DirectionQuery:
    direction: Literal["short", "long"]
    confirmed: bool
    formation: GateResult
    blockers: tuple[GateResult, ...]
    delivery_gate: GateResult | None
    delivery_tier: Any | None
    would_deliver: bool


@dataclass(frozen=True, slots=True)
class QueryResult:
    symbol: str
    row: dict[str, Any]
    source: str
    from_store: bool
    age_s: float | None
    short: DirectionQuery
    long: DirectionQuery
    focus_direction: Literal["short", "long"]

    def focus(self) -> DirectionQuery:
        return self.short if self.focus_direction == "short" else self.long


def row_age_seconds(row: dict[str, Any]) -> float | None:
    ts = row.get("ts")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds()
    except (TypeError, ValueError):
        return None


def _setup_conviction(setup: dict[str, Any], direction: str) -> float:
    from hunt_core.scanner.detect.setup_fields import setup_conviction_pct

    return setup_conviction_pct(setup, direction=direction)


def _pick_focus(row: dict[str, Any]) -> Literal["short", "long"]:
    v2 = row.get("verdict_v2")
    if v2 is not None:
        action = str(getattr(getattr(v2, "signal_decision", None), "action", "") or "")
        if action == "short":
            return "short"
        if action == "long":
            return "long"
    summary = row.get("verdict_v2_summary")
    if isinstance(summary, dict):
        action = str(summary.get("action") or "")
        if action == "short":
            return "short"
        if action == "long":
            return "long"
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    if dump.get("confirmed") and not long_setup.get("confirmed"):
        return "short"
    if long_setup.get("confirmed") and not dump.get("confirmed"):
        return "long"
    if dump.get("confirmed") and long_setup.get("confirmed"):
        return "short" if _setup_conviction(dump, "short") >= _setup_conviction(long_setup, "long") else "long"
    return "short"


def _dedupe_blockers(blockers: list[GateResult]) -> list[GateResult]:
    seen: set[str] = set()
    out: list[GateResult] = []
    for item in blockers:
        if item.code in seen:
            continue
        seen.add(item.code)
        out.append(item)
    return out


def _evaluate_direction(
    row: dict[str, Any],
    *,
    direction: Literal["short", "long"],
    symbol: str,
    lc: dict[str, Any],
    from_store: bool,
    sniper_config: Any,
) -> DirectionQuery:
    from hunt_core.deliver.dispatch import evaluate_delivery, evaluate_delivery_fast
    from hunt_core.scanner.detect.delivery_support import collect_report_blockers, evaluate_formation

    setup = (row.get("dump") if direction == "short" else row.get("long")) or {}
    confirmed = bool(setup.get("confirmed") or setup.get("intrabar_confirmed"))
    formation = evaluate_formation(
        setup, direction=direction, symbol=symbol, lifecycle=lc
    )
    blockers = _dedupe_blockers(
        collect_report_blockers(
            setup,
            direction=direction,
            symbol=symbol,
            lifecycle=lc,
            row=row,
            sniper_config=sniper_config,
            fast_lane=from_store,
        )
    )
    delivery_gate: GateResult | None = None
    delivery_tier: Any | None = None
    would_deliver = False
    if confirmed:
        use_fast = from_store or str(row.get("tick_path") or "") in _HOT_TICK_PATHS
        eval_fn = evaluate_delivery_fast if use_fast else evaluate_delivery
        delivery_gate, delivery_tier = eval_fn(
            row,
            direction=direction,
            setup=setup,
            lifecycle=lc,
            symbol=symbol,
            sniper_config=sniper_config,
            refresh_live_price=not from_store,
        )
        would_deliver = bool(delivery_gate.ok and delivery_tier is not None)
    return DirectionQuery(
        direction=direction,
        confirmed=confirmed,
        formation=formation,
        blockers=tuple(blockers),
        delivery_gate=delivery_gate,
        delivery_tier=delivery_tier,
        would_deliver=would_deliver,
    )


def build_query_result(
    row: dict[str, Any],
    symbol: str,
    *,
    source: str,
    from_store: bool,
    age_s: float | None,
    sniper_config: Any = None,
) -> QueryResult:
    sym = symbol.upper()
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    if sniper_config is None:
        from hunt_core.runtime.state import SNIPER_CONFIG

        sniper_config = SNIPER_CONFIG
    short_q = _evaluate_direction(
        row, direction="short", symbol=sym, lc=lc, from_store=from_store, sniper_config=sniper_config
    )
    long_q = _evaluate_direction(
        row, direction="long", symbol=sym, lc=lc, from_store=from_store, sniper_config=sniper_config
    )
    return QueryResult(
        symbol=sym,
        row=row,
        source=source,
        from_store=from_store,
        age_s=age_s,
        short=short_q,
        long=long_q,
        focus_direction=_pick_focus(row),
    )


async def resolve_query_row(
    symbol: str,
    *,
    live: bool = False,
    stagger_ms: int = 200,
    client: HuntCcxtClient | None = None,
) -> tuple[dict[str, Any], str, bool, float | None]:
    """Return ``(row, source, from_store, age_s)`` — DeepQueryStore first unless ``live``."""
    from hunt_core.runtime.deep_assembly import assemble_deep_tick
    from hunt_core.runtime.symbol_probe import normalize_symbol
    from hunt_core.runtime.tick_state import deep_query_store

    sym = normalize_symbol(symbol)
    row: dict[str, Any] | None = None
    from_store = False
    age_s: float | None = None
    source = "live_rest"

    if live:
        row = await assemble_deep_tick(sym, client, stagger_ms=max(stagger_ms, 200))
        source = "deep_live"
    else:
        cached = deep_query_store().resolve(sym)
        if isinstance(cached, dict) and not cached.get("error"):
            age_s = row_age_seconds(cached)
            if age_s is None or age_s <= STORE_STALE_S:
                row = cached
                from_store = True
                source = str(cached.get("tick_path") or "deep_store")
                if age_s is not None and age_s > STORE_FRESH_S:
                    row = dict(cached)
                    row["_stale_store"] = True

    if row is None:
        row = await assemble_deep_tick(sym, client, stagger_ms=max(stagger_ms, 200))
        source = "deep_assembly"

    if row.get("maps") and not row.get("maps_forecast"):
        from hunt_core.maps.forecast import stamp_forecasts_on_row

        stamp_forecasts_on_row(row)

    return row, source, from_store, age_s


def _format_blockers_section(dq: DirectionQuery) -> list[str]:
    lines: list[str] = []
    dir_ru = "ШОРТ" if dq.direction == "short" else "ЛОНГ"

    if dq.confirmed:
        if dq.would_deliver:
            tier = getattr(dq.delivery_tier, "tier", None) or (
                dq.delivery_tier.get("tier")
                if isinstance(dq.delivery_tier, dict)
                else None
            )
            tier_txt = f" · tier <code>{html.escape(str(tier))}</code>" if tier else ""
            lines.append(f"✅ <b>Delivery {dir_ru}</b>: прошёл бы сейчас{tier_txt}")
        elif dq.delivery_gate is not None:
            g = dq.delivery_gate
            lines.append(
                f"🚫 <b>Delivery {dir_ru}</b>: "
                f"<code>{html.escape(g.code or 'gate')}</code> — "
                f"<i>{html.escape(g.message or '')}</i>"
            )
    else:
        f = dq.formation
        lines.append(
            f"📋 <b>Forming {dir_ru}</b>: "
            f"<code>{html.escape(f.code or 'forming')}</code> — "
            f"<i>{html.escape(f.message or '')}</i>"
        )

    shown = 0
    for b in dq.blockers:
        if not b.ok and b.code != "ok":
            if not dq.confirmed and b.code == "not_confirmed":
                continue
            if dq.delivery_gate and b.code == dq.delivery_gate.code:
                continue
            lines.append(
                f"  • <code>{html.escape(b.code)}</code> — "
                f"<i>{html.escape((b.message or '')[:120])}</i>"
            )
            shown += 1
            if shown >= _MAX_BLOCKERS_SHOWN:
                rest = sum(1 for x in dq.blockers if not x.ok) - shown
                if rest > 0:
                    lines.append(f"  <i>…ещё {rest} blocker(s)</i>")
                break
    return lines


def format_query_telegram(q: QueryResult, *, added_watch: bool = False) -> str:
    """Deep-first /signal — analysis/deep structure/MTF/maps; hunt scan collapsed footer."""
    from hunt_core.deep.build import build_deep_report
    from hunt_core.deep.format_telegram import format_deep_analysis_telegram
    from hunt_core.runtime.tick_state import hunt_scan_store

    focus = q.focus()
    try:
        analysis = build_deep_report(q.row, include_watch_appendix=False)
        parts: list[str] = [format_deep_analysis_telegram(analysis)]
    except Exception:
        # Fail-loud: surface the real traceback to logs (was silently swallowed,
        # masking the verdict_v2-dict crash). User still gets a graceful card.
        import structlog

        structlog.get_logger("hunt_core.runtime.query_service").exception(
            "deep_report_build_failed", symbol=q.symbol, from_store=q.from_store
        )
        parts = [
            f"🔬 <b>Глубокий анализ — {html.escape(q.symbol)}</b>\n"
            "<i>анализ временно недоступен · /signal SYM --live для REST</i>"
        ]

    watch_lines: list[str] = []
    if added_watch:
        watch_lines.append("<i>+ watchlist</i>")
    hunt_row = hunt_scan_store().get(q.symbol)
    if isinstance(hunt_row, dict) and not hunt_row.get("error"):
        setup = (hunt_row.get("dump") if focus.direction == "short" else hunt_row.get("long")) or {}
        phase = str(setup.get("phase") or "—")
        confirmed = bool(setup.get("confirmed"))
        _DIR_RU = {"short": "шорт", "long": "лонг"}
        dir_ru = _DIR_RU.get(focus.direction, focus.direction)
        if confirmed:
            watch_lines.append(f"<i>Сканер: {dir_ru} подтверждён · фаза={html.escape(phase)}</i>")
        else:
            watch_lines.append(
                f"<i>Сканер: {dir_ru} формируется · {html.escape(phase)} · отдельный контур</i>"
            )
    else:
        watch_lines.append("<i>Сканер: нет тика по символу (динамический скан)</i>")
    if watch_lines:
        parts.extend(["", "—", "<b>Сканер</b> (Модуль 2, справочно)", *watch_lines])

    if q.from_store:
        stale = bool(q.row.get("_stale_store"))
        as_of = q.row.get("as_of") or (q.row.get("freshness") or {}).get("as_of")
        as_of_txt = ""
        if as_of:
            as_of_txt = f" · снимок {html.escape(str(as_of)[:19].replace('T', ' '))} UTC"
        age_txt = f"{q.age_s:.0f}s назад" if q.age_s is not None else "watch-тик"
        if stale:
            parts.append(
                f"\n<i>📊 данные {age_txt}{as_of_txt} (устарели) · обновляю в фоне · "
                f"/signal {q.symbol.replace('USDT', '')} --live для немедленного REST</i>"
            )
        else:
            parts.append(
                f"\n<i>📊 из watch-тика ({age_txt}{as_of_txt}) · "
                f"/signal {q.symbol.replace('USDT', '')} --live для REST</i>"
            )
    else:
        as_of = q.row.get("as_of")
        tail = f" · {html.escape(str(as_of)[:19].replace('T', ' '))} UTC" if as_of else ""
        parts.append(f"\n<i>🛰 {html.escape(q.source)}{tail}</i>")
    return "\n".join(parts)


def spawn_background_refresh(
    symbol: str,
    *,
    client: HuntCcxtClient | None = None,
    stagger_ms: int = 200,
) -> None:
    """Non-blocking REST refresh after a stale store hit — updates LastTickStore only."""
    import asyncio

    from hunt_core.runtime.tick_state import deep_query_store

    async def _run() -> None:
        try:
            row, _src, _store, _age = await resolve_query_row(
                symbol, live=True, stagger_ms=stagger_ms, client=client
            )
            if isinstance(row, dict) and not row.get("error"):
                deep_query_store().put(symbol.upper(), row)
        except Exception:
            pass

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
    except RuntimeError:
        pass


__all__ = [
    "DirectionQuery",
    "QueryResult",
    "STORE_FRESH_S",
    "build_query_result",
    "format_query_telegram",
    "resolve_query_row",
    "row_age_seconds",
    "spawn_background_refresh",
]

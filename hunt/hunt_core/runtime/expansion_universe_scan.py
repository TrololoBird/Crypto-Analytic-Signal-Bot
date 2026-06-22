"""Expansion universe scan loop — TOP-N watch alerts + scan jsonl."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from hunt_core._dev.expansion_lab.config import ExpansionConfig, load_expansion_config
from hunt_core._dev.expansion_lab.ranking.scan import rank_universe
from hunt_core._dev.expansion_lab.types import ExpansionOpportunity
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.paths import EXPANSION_SCAN_JSONL
from hunt_core.runtime.expansion_alerts import (
    expansion_alert_eligible,
    expansion_change_fingerprint,
    expansion_cooldown_ok,
    last_alert_fingerprint,
    mark_expansion_alert_sent,
)

LOG = structlog.get_logger("hunt.expansion_universe_scan")


def collect_universe_rows() -> dict[str, dict[str, Any]]:
    """Merge hunt scan + deep query caches (deep wins on conflict)."""
    from hunt_core.runtime.tick_state import deep_query_store, hunt_scan_store

    rows: dict[str, dict[str, Any]] = {}
    for store in (hunt_scan_store(), deep_query_store()):
        for row in store.all_rows():
            sym = str(row.get("symbol") or "").upper()
            if sym and not row.get("error"):
                rows[sym] = row
    return rows


def should_universe_alert(
    opp: ExpansionOpportunity,
    cfg: ExpansionConfig,
    *,
    now: datetime | None = None,
) -> bool:
    """Eligible, above opp floor, cooldown clear, fingerprint changed; skip pinned."""
    sym = str(opp.symbol or "").upper()
    if not sym or sym in PINNED_SYMBOLS:
        return False
    if opp.meta.opportunity_score < cfg.tg_universe_min_opp:
        return False
    exp = opp.to_dict()
    if not expansion_alert_eligible(exp, cfg):
        return False
    if not expansion_cooldown_ok(sym, cfg, now=now):
        return False
    fp = expansion_change_fingerprint(exp)
    if fp and fp == last_alert_fingerprint(sym):
        return False
    return True


def select_universe_alerts(
    lists: dict[str, list[ExpansionOpportunity]],
    cfg: ExpansionConfig,
    *,
    now: datetime | None = None,
) -> dict[str, list[ExpansionOpportunity]]:
    """Filter ranked lists to symbols that should fire a universe alert now."""
    out: dict[str, list[ExpansionOpportunity]] = {"pre_pump": [], "pre_dump": []}
    for side in ("pre_pump", "pre_dump"):
        for opp in lists.get(side) or []:
            if should_universe_alert(opp, cfg, now=now):
                out[side].append(opp)
    return out


def write_expansion_scan_jsonl(lists: dict[str, list[ExpansionOpportunity]]) -> None:
    try:
        EXPANSION_SCAN_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with EXPANSION_SCAN_JSONL.open("w", encoding="utf-8") as fh:
            for side, opps in lists.items():
                for opp in opps:
                    rec = opp.to_dict()
                    rec["scan_side"] = side
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


async def send_universe_alert_telegram(
    broadcaster: Any,
    alerts: dict[str, list[ExpansionOpportunity]],
) -> bool:
    from hunt_core._dev.expansion_lab.format import format_universe_alert

    pump = alerts.get("pre_pump") or []
    dump = alerts.get("pre_dump") or []
    if not pump and not dump:
        return False
    text = format_universe_alert(alerts)
    result = await broadcaster.send_html(text, no_split=True)
    if result.status == "sent":
        LOG.info(
            "expansion_universe_tg_sent",
            pre_pump=len(pump),
            pre_dump=len(dump),
            message_id=result.message_id,
        )
        return True
    LOG.warning(
        "expansion_universe_tg_failed",
        status=result.status,
        reason=result.reason,
    )
    return False


def _record_universe_alerts(
    alerts: dict[str, list[ExpansionOpportunity]],
    *,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    for opps in alerts.values():
        for opp in opps:
            sym = str(opp.symbol or "").upper()
            fp = expansion_change_fingerprint(opp.to_dict())
            mark_expansion_alert_sent(sym, fingerprint=fp, now=now)
            try:
                from hunt_core._dev.expansion_lab.learning import record_expansion_signal

                if opp.dominant != "neutral" and opp.meta.expansion_quality >= 0.45:
                    record_expansion_signal(opp, ts=now.isoformat())
            except Exception:
                LOG.debug("expansion_universe_record_failed", symbol=sym, exc_info=True)


async def run_universe_scan_once(
    broadcaster: Any | None,
    *,
    cfg: ExpansionConfig | None = None,
    send_telegram: bool = True,
) -> dict[str, Any]:
    """One universe rank + optional batched TG alert."""
    cfg = cfg or load_expansion_config()
    rows = collect_universe_rows()
    summary: dict[str, Any] = {
        "rows": len(rows),
        "alerted_pump": 0,
        "alerted_dump": 0,
        "sent": False,
    }
    if not rows or not cfg.enabled:
        return summary

    ranked = rank_universe(rows.values(), cfg=cfg, top_n=cfg.tg_universe_top_n)
    write_expansion_scan_jsonl(ranked)

    if not cfg.tg_universe_scan:
        return summary

    now = datetime.now(UTC)
    alerts = select_universe_alerts(ranked, cfg, now=now)
    summary["alerted_pump"] = len(alerts.get("pre_pump") or [])
    summary["alerted_dump"] = len(alerts.get("pre_dump") or [])

    if send_telegram and broadcaster is not None and (
        summary["alerted_pump"] or summary["alerted_dump"]
    ):
        if await send_universe_alert_telegram(broadcaster, alerts):
            summary["sent"] = True
            _record_universe_alerts(alerts, now=now)

    return summary


async def expansion_universe_scan_loop(
    broadcaster: Any | None,
    *,
    interval_s: float | None = None,
    send_telegram: bool = True,
) -> None:
    """Background TOP-N universe scan — batched expansion alerts on watch rows."""
    from hunt_core.runtime.state import should_stop

    import asyncio

    cfg = load_expansion_config()
    if not cfg.enabled or not cfg.tg_universe_scan:
        LOG.info("expansion_universe_scan_disabled")
        return

    interval = interval_s if interval_s is not None else cfg.tg_universe_interval_s
    LOG.info("expansion_universe_scan_start", interval_s=interval, top_n=cfg.tg_universe_top_n)
    while not should_stop():
        try:
            summary = await run_universe_scan_once(
                broadcaster,
                cfg=cfg,
                send_telegram=send_telegram,
            )
            if summary.get("sent"):
                LOG.info("expansion_universe_scan_tick", **summary)
        except Exception:
            LOG.exception("expansion_universe_scan_failed")
        try:
            from hunt_core._dev.expansion_lab.runtime_state import save_expansion_runtime_state

            save_expansion_runtime_state()
        except Exception:
            LOG.debug("expansion_fsm_save_failed", exc_info=True)
        try:
            await asyncio.sleep(max(120.0, interval))
        except asyncio.CancelledError:
            break
    LOG.info("expansion_universe_scan_stop")


__all__ = [
    "collect_universe_rows",
    "expansion_universe_scan_loop",
    "run_universe_scan_once",
    "select_universe_alerts",
    "should_universe_alert",
    "write_expansion_scan_jsonl",
]

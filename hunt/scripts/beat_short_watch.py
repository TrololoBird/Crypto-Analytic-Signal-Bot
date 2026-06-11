#!/usr/bin/env python3
"""Independent BEAT short monitor — 60s REST refresh, Telegram on indie confirm."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from hunt_watch.bootstrap import bootstrap

bootstrap()

from engine.domain.config import load_settings
from engine.telegram import TelegramBroadcaster
from hunt_watch.beat_dump_lab import TickState, make_client, run_tick
from hunt_watch.independent_short import (
    cooldown_open,
    evaluate_independent_short,
    format_telegram_short,
    load_watch_state,
    mark_sent,
    save_watch_state,
    status_line,
)
from hunt_watch.paths import DATA
from hunt_watch.scriptutil import configure_script_logging

OUT_DIR = DATA / "experiments" / "beat_short_watch"
JSONL = OUT_DIR / "ticks.jsonl"
SNAPSHOT = OUT_DIR / "latest.json"
LOG = configure_script_logging("beat_short_watch")

_STOP = False


def _handle_stop(*_args: object) -> None:
    global _STOP
    _STOP = True


async def _send_telegram(symbol: str, row: dict, verdict) -> bool:
    settings = load_settings()
    if not settings.tg_token or not settings.target_chat_id:
        LOG.warning("telegram_not_configured")
        return False
    broadcaster = TelegramBroadcaster(settings.tg_token, settings.target_chat_id)
    try:
        await broadcaster.preflight_check()
        msg = format_telegram_short(symbol, row, verdict)
        result = await broadcaster.send_html(msg)
        if result.status == "sent":
            LOG.info("telegram_sent", symbol=symbol, message_id=result.message_id)
            return True
        LOG.warning("telegram_failed", status=result.status, reason=result.reason)
        return False
    finally:
        await broadcaster.close()


async def _tick_once(
    symbol: str,
    *,
    prior: TickState | None,
    tg_state: dict,
    send_telegram: bool,
) -> TickState | None:
    client = make_client()
    try:
        row, new_prior = await run_tick(client, symbol, prior=prior)
    finally:
        await client.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    matrix = row.get("feature_matrix") or {}
    indie_prior = {
        "rsi5_closed": tg_state.get("prev_rsi5_closed"),
        "rsi15_closed": tg_state.get("prev_rsi15_closed"),
    }
    verdict = evaluate_independent_short(row, prior=indie_prior)
    rsi5_now = (matrix.get("5m_closed") or {}).get("rsi14")
    rsi15_now = (matrix.get("15m_closed") or {}).get("rsi14")
    if rsi5_now is not None:
        tg_state["prev_rsi5_closed"] = rsi5_now
    if rsi15_now is not None:
        tg_state["prev_rsi15_closed"] = rsi15_now
    row["independent_short"] = {
        "confirmed": verdict.confirmed,
        "setup": verdict.setup,
        "reasons": verdict.reasons,
        "blocks": verdict.blocks,
        "levels": verdict.levels,
    }

    SNAPSHOT.write_text(json.dumps(row, indent=2, default=str), encoding="utf-8")
    with JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")

    LOG.info(
        "beat_short_tick",
        symbol=symbol,
        confirmed=verdict.confirmed,
        setup=verdict.setup,
        price=row.get("price"),
        blocks=verdict.blocks[:3],
    )
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {status_line(row, verdict)}", flush=True)

    if verdict.confirmed and send_telegram and cooldown_open(symbol, tg_state):
        if await _send_telegram(symbol, row, verdict):
            mark_sent(symbol, tg_state)
            save_watch_state(tg_state)

    return new_prior


async def run_loop(
    symbol: str,
    *,
    interval_s: int,
    send_telegram: bool,
) -> int:
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    tg_state = load_watch_state()
    prior: TickState | None = None
    if SNAPSHOT.exists():
        try:
            last = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
            m = last.get("feature_matrix") or {}
            prior = TickState(
                ts=str(last.get("ts") or ""),
                cluster_scores=dict(last.get("cluster_scores") or {}),
                composite=float(last.get("composite_dump_score") or 0),
            )
            if (m.get("5m_closed") or {}).get("rsi14") is not None:
                tg_state.setdefault("prev_rsi5_closed", (m.get("5m_closed") or {}).get("rsi14"))
            if (m.get("15m_closed") or {}).get("rsi14") is not None:
                tg_state.setdefault("prev_rsi15_closed", (m.get("15m_closed") or {}).get("rsi14"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            prior = None

    LOG.info("beat_short_watch_start", symbol=symbol, interval_s=interval_s, telegram=send_telegram)
    print(
        f"Independent short watch {symbol} every {interval_s}s · "
        f"Telegram={'on' if send_telegram else 'off'} · Ctrl+C stop",
        flush=True,
    )

    while not _STOP:
        try:
            prior = await _tick_once(symbol, prior=prior, tg_state=tg_state, send_telegram=send_telegram)
            save_watch_state(tg_state)
        except Exception:
            LOG.exception("beat_short_tick_failed")
        if _STOP:
            break
        await asyncio.sleep(max(10, interval_s))

    LOG.info("beat_short_watch_stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent BEAT short monitor")
    parser.add_argument("--symbol", default="BEATUSDT")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    args = parser.parse_args()
    sym = args.symbol.upper()

    if args.once:

        async def _once() -> int:
            tg = load_watch_state()
            await _tick_once(sym, prior=None, tg_state=tg, send_telegram=not args.no_telegram)
            return 0

        return asyncio.run(_once())

    return asyncio.run(
        run_loop(sym, interval_s=max(30, args.interval), send_telegram=not args.no_telegram)
    )


if __name__ == "__main__":
    raise SystemExit(main())

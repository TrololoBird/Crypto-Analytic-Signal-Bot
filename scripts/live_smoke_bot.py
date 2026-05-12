from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import structlog

from bot.application.bot import SignalBot
from bot.domain.config import load_settings
from bot.messaging import DeliveryResult


LOG = structlog.get_logger("scripts.live_smoke_bot")


class FakeBroadcaster:
    async def preflight_check(self) -> None:
        return None

    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> DeliveryResult:
        return DeliveryResult(status="suppressed", message_id=None, reason="live_smoke_bot")

    async def edit_html(self, message_id: int, text: str) -> None:
        return None

    async def close(self) -> None:
        return None


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        force=True,
    )


def _fetch_active_signal_row(db_path: Path, tracking_id: str) -> dict[str, Any] | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT tracking_id, status, pending_expires_at, active_expires_at, activated_at, closed_at, close_reason "
            "FROM active_signals WHERE tracking_id = ?",
            (tracking_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


async def _run(
    tracking_id: str,
    warmup_seconds: float,
    *,
    runtime_seconds: float = 0.0,
    shutdown_timeout_seconds: float = 60.0,
    force_exit_on_close_timeout: bool = False,
) -> None:
    os.environ.setdefault("BOT_DISABLE_HTTP_SERVERS", "1")
    settings = load_settings()
    before = _fetch_active_signal_row(settings.db_path, tracking_id)
    LOG.info("tracking_row_before_start", row=before)

    bot = SignalBot(settings, broadcaster=FakeBroadcaster())
    runtime_task: asyncio.Task[None] | None = None
    try:
        await bot.start()
        if runtime_seconds > 0.0:
            runtime_task = asyncio.create_task(bot.run_forever(), name="live_smoke_runtime")
            await asyncio.sleep(runtime_seconds)
        else:
            await asyncio.sleep(warmup_seconds)
        summary = await bot._run_emergency_cycle()
        ws_snapshot = bot._ws_manager.state_snapshot() if bot._ws_manager is not None else {}
        after = _fetch_active_signal_row(settings.db_path, tracking_id)
        LOG.info("tracking_row_after_start", row=after)
        LOG.info(
            "live_smoke_summary",
            prepare_error_count=bot._prepare_error_count,
            ws_snapshot=ws_snapshot,
            emergency_cycle_summary=summary,
        )
        if before is not None and before.get("status") in {"pending", "active"}:
            if after is not None and after.get("status") in {"pending", "active"}:
                raise RuntimeError(
                    f"startup sweep did not close expired tracked signal: before={before} after={after}"
                )
        if bot._prepare_error_count != 0:
            raise RuntimeError(
                f"prepare errors observed during live smoke: {bot._prepare_error_count}"
            )
        if int(ws_snapshot.get("fresh_tickers") or 0) <= 0:
            raise RuntimeError(f"fresh_tickers missing in live smoke snapshot: {ws_snapshot}")
        if int(ws_snapshot.get("fresh_mark_prices") or 0) <= 0:
            raise RuntimeError(f"fresh_mark_prices missing in live smoke snapshot: {ws_snapshot}")
    finally:
        bot.request_shutdown()
        if runtime_task is not None:
            try:
                await asyncio.wait_for(runtime_task, timeout=shutdown_timeout_seconds)
            except TimeoutError:
                LOG.warning(
                    "runtime task did not stop within timeout; cancelling",
                    timeout_seconds=shutdown_timeout_seconds,
                )
                runtime_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runtime_task
        try:
            await asyncio.wait_for(bot.close(), timeout=shutdown_timeout_seconds)
        except TimeoutError:
            LOG.warning(
                "bot close timed out after live smoke summary",
                timeout_seconds=shutdown_timeout_seconds,
            )
            if force_exit_on_close_timeout:
                logging.shutdown()
                os._exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="End-to-end live smoke test without Telegram sends"
    )
    parser.add_argument(
        "--tracking-id",
        default="XRPUSDT|structure_pullback|long|20260421T131017986805Z",
    )
    parser.add_argument("--warmup-seconds", type=float, default=20.0)
    parser.add_argument(
        "--runtime-seconds",
        type=float,
        default=0.0,
        help="Run full EventBus/background runtime for this many seconds before the final emergency cycle.",
    )
    parser.add_argument(
        "--shutdown-timeout-seconds",
        type=float,
        default=60.0,
        help="Maximum seconds to wait for runtime shutdown and resource close.",
    )
    parser.add_argument(
        "--force-exit-on-close-timeout",
        action="store_true",
        help="Hard-exit after a close timeout once the live smoke summary has been written.",
    )
    args = parser.parse_args()

    _configure_logging()
    asyncio.run(
        _run(
            args.tracking_id,
            args.warmup_seconds,
            runtime_seconds=max(0.0, float(args.runtime_seconds)),
            shutdown_timeout_seconds=max(1.0, float(args.shutdown_timeout_seconds)),
            force_exit_on_close_timeout=bool(args.force_exit_on_close_timeout),
        )
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sqlite3
import tracemalloc
from typing import TYPE_CHECKING, Any

import structlog

try:
    from scripts.clean_session_data import clean_session_artifacts
    from scripts.smoke_fail_fast import (
        SmokeFailFastError,
        SmokeFailFastGuard,
        install_asyncio_exception_logging,
        wait_for_runtime_or_abort,
    )
except ModuleNotFoundError:  # pragma: no cover
    from clean_session_data import clean_session_artifacts
    from smoke_fail_fast import (
        SmokeFailFastError,
        SmokeFailFastGuard,
        install_asyncio_exception_logging,
        wait_for_runtime_or_abort,
    )

try:
    import scripts.common  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    import common  # noqa: F401

from bot.cli import configure_logging
from bot.runtime.bot import SignalBot
from engine.domain.config import load_settings
from engine.telegram import DeliveryResult

if TYPE_CHECKING:
    from pathlib import Path

    from engine.market.ws import FuturesWSManager

LOG = structlog.get_logger("scripts.live_smoke_bot")
_MISSING_LOG_VALUE = "not_available"


class FakeBroadcaster:
    def __init__(self) -> None:
        self._message_id = 0

    async def preflight_check(self) -> None:
        return None

    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> DeliveryResult:
        del text, reply_to_message_id
        self._message_id += 1
        return DeliveryResult(status="sent", message_id=self._message_id, reason="live_smoke_bot")

    async def edit_html(self, message_id: int, text: str) -> None:
        del message_id, text
        return

    async def close(self) -> None:
        return None


def _configure_logging(*, debug: bool = False) -> None:
    settings = load_settings()
    if debug:
        os.environ["DEBUG_BOT"] = "1"
    configure_logging(
        settings, debug_mode=debug or os.getenv("DEBUG_BOT", "0") in ("1", "true", "yes")
    )
    logging.captureWarnings(capture=True)


def _install_asyncio_exception_logging(guard: SmokeFailFastGuard | None = None) -> None:
    install_asyncio_exception_logging(guard=guard)


def _fetch_active_signal_row(db_path: Path, tracking_id: str) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT tracking_id, status, pending_expires_at, active_expires_at, "
            "activated_at, closed_at, close_reason "
            "FROM active_signals WHERE tracking_id = ?",
            (tracking_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _sanitize_log_value(value: Any) -> Any:
    if value is None:
        return _MISSING_LOG_VALUE
    if isinstance(value, dict):
        return {str(k): _sanitize_log_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_log_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_log_value(item) for item in value)
    return value


async def _wait_for_mark_prices(
    ws_manager: FuturesWSManager, timeout_seconds: float
) -> dict[str, object]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last: dict[str, object] = {}
    while loop.time() < deadline:
        last = ws_manager.state_snapshot()
        if int(last.get("fresh_mark_prices") or 0) > 0:
            return last
        await asyncio.sleep(1.0)
    return last


async def _run(
    tracking_id: str,
    warmup_seconds: float,
    *,
    runtime_seconds: float = 0.0,
    shutdown_timeout_seconds: float = 60.0,
    final_emergency_timeout_seconds: float = 180.0,
    force_exit_on_close_timeout: bool = False,
    run_final_emergency_cycle: bool = True,
    fail_fast: bool = True,
    disable_http: bool = False,
) -> None:
    if disable_http:
        os.environ["BOT_DISABLE_HTTP_SERVERS"] = "1"
        os.environ["BOT_DISABLE_DASHBOARD"] = "1"
    else:
        os.environ.pop("BOT_DISABLE_HTTP_SERVERS", None)
        os.environ.pop("BOT_DISABLE_DASHBOARD", None)
    settings = load_settings()
    before = _fetch_active_signal_row(settings.db_path, tracking_id)
    LOG.info(
        "tracking_row_before_start",
        row_present=before is not None,
        row=_sanitize_log_value(before) if before is not None else {},
    )

    loop = asyncio.get_running_loop()
    abort_event = asyncio.Event()
    fail_fast_guard = SmokeFailFastGuard(loop=loop, abort_event=abort_event, enabled=fail_fast)
    fail_fast_guard.install()

    bot = SignalBot(settings, broadcaster=FakeBroadcaster())
    runtime_task: asyncio.Task[None] | None = None
    try:
        _install_asyncio_exception_logging(fail_fast_guard if fail_fast else None)
        await bot.start()
        if not disable_http:
            host = settings.runtime.dashboard_host or "127.0.0.1"
            port = settings.runtime.dashboard_port
            LOG.info(
                "live_smoke_dashboard",
                url=f"http://{host}:{port}/",
                metrics_port=9090,
            )
        fail_fast_guard.mark_startup_complete()
        fail_fast_guard.raise_if_aborted()
        if runtime_seconds > 0.0:
            runtime_task = asyncio.create_task(bot.run_forever(), name="live_smoke_runtime")
            if await wait_for_runtime_or_abort(runtime_seconds, abort_event):
                LOG.error(
                    "live_smoke_fail_fast_abort",
                    reason="runtime_window_interrupted",
                    runtime_seconds=float(runtime_seconds),
                )
                fail_fast_guard.raise_if_aborted()
        else:
            if await wait_for_runtime_or_abort(warmup_seconds, abort_event):
                LOG.error(
                    "live_smoke_fail_fast_abort",
                    reason="warmup_window_interrupted",
                    warmup_seconds=float(warmup_seconds),
                )
                fail_fast_guard.raise_if_aborted()
        if abort_event.is_set():
            fail_fast_guard.raise_if_aborted()
        if run_final_emergency_cycle:
            try:
                summary = await asyncio.wait_for(
                    bot._run_emergency_cycle(),
                    timeout=final_emergency_timeout_seconds,
                )
            except TimeoutError:
                summary = {
                    "executed": False,
                    "reason": "final_emergency_cycle_timeout",
                    "timeout_seconds": float(final_emergency_timeout_seconds),
                }
                LOG.info(
                    "final_emergency_cycle_timeout",
                    timeout_seconds=float(final_emergency_timeout_seconds),
                )
        else:
            summary = {
                "executed": False,
                "reason": "skipped_after_runtime_window",
                "runtime_seconds": float(runtime_seconds),
            }
        ws_snapshot = bot._ws_manager.state_snapshot() if bot._ws_manager is not None else {}
        after = _fetch_active_signal_row(settings.db_path, tracking_id)
        LOG.info(
            "tracking_row_after_start",
            row_present=after is not None,
            row=_sanitize_log_value(after) if after is not None else {},
        )
        if bot._ws_manager is not None and int(ws_snapshot.get("fresh_mark_prices") or 0) <= 0:
            ws_snapshot = await _wait_for_mark_prices(bot._ws_manager, timeout_seconds=30.0)
        LOG.info(
            "live_smoke_summary",
            prepare_error_count=bot._prepare_error_count,
            ws_snapshot=_sanitize_log_value(ws_snapshot),
            emergency_cycle_summary=_sanitize_log_value(summary),
        )
        if (
            before is not None
            and before.get("status") in {"pending", "active"}
            and after is not None
            and after.get("status") in {"pending", "active"}
        ):
            msg = (
                f"startup sweep did not close expired tracked signal: before={before} after={after}"
            )
            raise RuntimeError(msg)
        if bot._prepare_error_count != 0:
            msg = f"prepare errors observed during live smoke: {bot._prepare_error_count}"
            raise RuntimeError(msg)
        ticker_ok = int(ws_snapshot.get("fresh_tickers") or 0) > 0
        if not ticker_ok and bot._ws_manager is not None:
            ticker_ok = bool(bot._ws_manager.is_ticker_cache_warm())
        mark_ok = int(ws_snapshot.get("fresh_mark_prices") or 0) > 0
        if not ticker_ok and not mark_ok:
            msg = f"ticker/market cache not warm in live smoke snapshot: {ws_snapshot}"
            raise RuntimeError(msg)
        if not mark_ok:
            msg = f"mark price cache not warm in live smoke snapshot: {ws_snapshot}"
            raise RuntimeError(msg)
    finally:
        fail_fast_guard.uninstall()
        bot.request_shutdown()
        if runtime_task is not None:
            try:
                await asyncio.wait_for(runtime_task, timeout=shutdown_timeout_seconds)
            except TimeoutError:
                LOG.exception(
                    "runtime task did not stop within timeout; cancelling",
                    timeout_seconds=shutdown_timeout_seconds,
                )
                runtime_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runtime_task
        try:
            await asyncio.wait_for(bot.close(), timeout=shutdown_timeout_seconds)
        except TimeoutError:
            LOG.exception(
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
        help=(
            "Run full EventBus/background runtime for this many seconds "
            "before the final emergency cycle."
        ),
    )
    parser.add_argument(
        "--shutdown-timeout-seconds",
        type=float,
        default=60.0,
        help="Maximum seconds to wait for runtime shutdown and resource close.",
    )
    parser.add_argument(
        "--final-emergency-timeout-seconds",
        type=float,
        default=180.0,
        help="Maximum seconds to wait for the optional final emergency cycle.",
    )
    parser.add_argument(
        "--force-exit-on-close-timeout",
        action="store_true",
        help="Hard-exit after a close timeout once the live smoke summary has been written.",
    )
    parser.add_argument(
        "--skip-final-emergency-cycle",
        action="store_true",
        help=(
            "Only validate the configured live runtime window; do not add an extra emergency cycle."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="DEBUG logs with func:line, asyncio loop debug, file log under data/bot/logs/",
    )
    parser.add_argument(
        "--keep-session-data",
        action="store_true",
        help="Do not wipe telemetry/live_watch/logs before the smoke run.",
    )
    parser.add_argument(
        "--clean-mode",
        choices=("telemetry", "smoke", "full"),
        default="full",
        help="Session cleanup mode before smoke (ignored with --keep-session-data).",
    )
    parser.add_argument(
        "--no-fail-fast",
        action="store_true",
        help="Keep running the full runtime window even after WARNING/ERROR logs appear.",
    )
    parser.add_argument(
        "--no-http",
        action="store_true",
        help="Disable embedded dashboard (:8080) and metrics (:9090) for this smoke run.",
    )
    args = parser.parse_args()

    settings = load_settings()
    if not args.keep_session_data:
        clean_session_artifacts(settings, mode=args.clean_mode)
    _configure_logging(debug=bool(args.debug))
    if args.debug:
        tracemalloc.start(25)
    try:
        asyncio.run(
            _run(
                args.tracking_id,
                args.warmup_seconds,
                runtime_seconds=max(0.0, float(args.runtime_seconds)),
                shutdown_timeout_seconds=max(1.0, float(args.shutdown_timeout_seconds)),
                final_emergency_timeout_seconds=max(
                    1.0, float(args.final_emergency_timeout_seconds)
                ),
                force_exit_on_close_timeout=bool(args.force_exit_on_close_timeout),
                run_final_emergency_cycle=not bool(args.skip_final_emergency_cycle),
                fail_fast=not bool(args.no_fail_fast),
                disable_http=bool(args.no_http),
            )
        )
    except SmokeFailFastError:
        LOG.exception("live_smoke_fail_fast_exit")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

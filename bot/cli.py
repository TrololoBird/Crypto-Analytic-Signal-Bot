"""Live-only entry point - the bot always runs in full live mode.

No dry-run, no once, no self-check. To verify connectivity, read the logs
and telemetry. To test, run the bot and watch real data.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from bot.domain.research_harvest import activate_research_harvest, apply_research_harvest_profile
from bot.runtime.errors import DEFENSIVE_EXC

from . import BotSettings, SignalBot, load_settings
from .logging_config import configure_structlog
from .migrations import migrate_db
from .ops.pid_utils import pid_is_alive as _pid_is_alive
from .ops.startup_report import generate_and_send_startup_report, run_daily_summary_loop
from .persistence.repository.memory import MemoryRepository
from .telemetry import TelemetryStore, run_dir_started_at

_LOGGER_STDERR_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d{3})?\s+\|")


def _configure_stdio_for_unicode() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def _get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop


def _is_preformatted_log_stderr(line: str) -> bool:
    return bool(_LOGGER_STDERR_PREFIX_RE.match(line.lstrip()))


def _run_doctor_check(settings: BotSettings) -> None:
    """Run startup doctor diagnostics (fail-fast on critical issues)."""
    from bot.diagnostics.startup_doctor import run_startup_doctor
    from bot.strategies import STRATEGY_CLASSES

    registered = {
        str(getattr(cls, "setup_id", "") or "").strip()
        for cls in STRATEGY_CLASSES
        if str(getattr(cls, "setup_id", "") or "").strip()
    }
    try:
        run_startup_doctor(settings, registered_setup_ids=registered)
    except DEFENSIVE_EXC:
        logging.getLogger("bot.cli").exception("DOCTOR FAILED")
        raise


def _bootstrap_env_if_missing() -> None:
    env_path = Path(".env")
    if env_path.exists():
        return
    env_path.write_text(
        "# Required Telegram runtime values\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n",
        encoding="utf-8",
    )
    print("[INFO] .env not found - created with empty TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")


def prune_run_dirs(
    runs_dir: Path,
    *,
    keep: int,
    retention_days: int,
    now: datetime | None = None,
) -> int:
    """Remove old telemetry run directories using ``run_metadata.started_at`` ordering."""
    anchor = now or datetime.now(UTC)
    if not runs_dir.exists():
        return 0
    entries = sorted(
        (path for path in runs_dir.iterdir() if path.is_dir()),
        key=run_dir_started_at,
        reverse=True,
    )
    removed = 0
    retention_delta = timedelta(days=max(1, int(retention_days)))
    for idx, path in enumerate(entries):
        age = anchor - run_dir_started_at(path)
        if idx < keep and age <= retention_delta:
            continue
        try:
            shutil.rmtree(path, ignore_errors=False)
            removed += 1
        except OSError:
            continue
    return removed


def _cleanup_runtime_artifacts(settings: BotSettings) -> None:
    logger = logging.getLogger("bot.cli")
    now = datetime.now(UTC)

    def _safe_mtime(path: Path) -> datetime:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            return now

    def _prune_files(
        directory: Path,
        *,
        pattern: str,
        keep: int,
        retention_days: int,
    ) -> int:
        if not directory.exists():
            return 0
        entries = sorted(
            (path for path in directory.glob(pattern) if path.is_file()),
            key=_safe_mtime,
            reverse=True,
        )
        removed = 0
        retention_delta = timedelta(days=max(1, int(retention_days)))
        for idx, path in enumerate(entries):
            age = now - _safe_mtime(path)
            if idx < keep and age <= retention_delta:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    logs_removed = _prune_files(
        settings.logs_dir,
        pattern="bot_*.log",
        keep=max(10, int(settings.runtime.logs_max_files)),
        retention_days=max(1, int(settings.runtime.logs_retention_days)),
    )
    runs_removed = prune_run_dirs(
        settings.telemetry_dir / "runs",
        keep=max(10, int(settings.runtime.telemetry_max_runs)),
        retention_days=max(1, int(settings.runtime.telemetry_retention_days)),
        now=now,
    )
    if logs_removed or runs_removed:
        logger.info(
            "runtime artifact cleanup | logs_removed=%d telemetry_runs_removed=%d",
            logs_removed,
            runs_removed,
        )


def _rotate_session_log(log_path: Path, *, stamp: str) -> None:
    if not log_path.exists():
        return
    try:
        if log_path.stat().st_size <= 0:
            return
    except OSError:
        return
    target = log_path.with_name(f"{log_path.stem}_{stamp}{log_path.suffix}")
    counter = 1
    while target.exists():
        target = log_path.with_name(f"{log_path.stem}_{stamp}.{counter}{log_path.suffix}")
        counter += 1
    try:
        shutil.move(str(log_path), str(target))
    except OSError as exc:
        sys.stderr.write(f"[ERROR] failed to rotate previous session log {log_path}: {exc}\n")


def configure_logging(settings: BotSettings, *, debug_mode: bool = False) -> None:
    session_dt = datetime.now(UTC)
    session_stamp = session_dt.strftime("%Y%m%d_%H%M%S")
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    # Per-session log file with unique name — open line-buffered (buffering=1) so every
    # log record is flushed immediately even when asyncio tasks are the sole writers.
    log_path = settings.logs_dir / f"bot_{session_stamp}_{os.getpid()}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _log_file = open(log_path, "w", buffering=1, encoding="utf-8")  # noqa: WPS515
        handlers.append(logging.StreamHandler(_log_file))
    except OSError as exc:
        sys.stderr.write(f"[ERROR] file logging disabled for {log_path}: {exc}\n")

    # Use DEBUG level for full traces
    log_level = logging.DEBUG if debug_mode else getattr(logging, settings.log_level, logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(funcName)s:%(lineno)d | %(message)s",
        handlers=handlers,
        force=True,
    )
    configure_structlog(log_level)

    if debug_mode:
        logging.getLogger("bot").setLevel(logging.DEBUG)
        logging.getLogger("scripts").setLevel(logging.DEBUG)

    loop = _get_or_create_event_loop()
    # Full asyncio task/callback traces only in explicit debug mode.
    loop.set_debug(bool(debug_mode))

    # Reduce noise from external libraries but keep warnings
    logging.getLogger("websockets").setLevel(logging.INFO)
    logging.getLogger("hpack").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)  # Only real warnings, not debug spam
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)  # Very noisy at DEBUG
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("charset_normalizer").setLevel(logging.WARNING)

    # Log file location
    start_time = session_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    logger = logging.getLogger("bot.cli")
    logger.info("=" * 80)
    logger.info("BOT SESSION STARTED | %s", start_time)
    logger.info("LOG FILE | %s", log_path)
    logger.info("DEBUG MODE | %s", debug_mode)
    logger.info("ASYNCIO DEBUG | %s", loop.get_debug())
    logger.info("=" * 80)


def _read_pid_value(pid_file: Path) -> int:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


async def _acquire_pid_lock(pid_file: Path) -> None:
    """Acquire PID lock asynchronously without blocking event loop."""
    # Run blocking filesystem operations in thread pool
    await asyncio.to_thread(pid_file.parent.mkdir, parents=True, exist_ok=True)

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    retries = 0
    while True:
        try:
            # Try to create lock file atomically
            fd = await asyncio.to_thread(os.open, pid_file, flags)
            try:
                await asyncio.to_thread(
                    os.write, fd, str(os.getpid()).encode("ascii", errors="strict")
                )
            finally:
                await asyncio.to_thread(os.close, fd)
        except FileExistsError:
            existing_pid = await asyncio.to_thread(_read_pid_value, pid_file)
            if existing_pid and existing_pid != os.getpid() and _pid_is_alive(existing_pid):
                msg = f"another bot process is already running with pid {existing_pid}"
                raise SystemExit(msg) from None
            # Race hardening for empty/initializing PID file:
            # never unlink immediately; first give other process enough time
            # to finish writing its PID.
            if existing_pid == 0:
                retries += 1
                try:
                    stat = await asyncio.to_thread(pid_file.stat)
                    age_s = max(0.0, time.time() - stat.st_mtime)
                except OSError:
                    age_s = 0.0
                if retries <= 50 or age_s < 10.0:
                    await asyncio.sleep(0.1)
                    continue
            try:
                await asyncio.to_thread(pid_file.unlink)
            except FileNotFoundError:
                continue
            except OSError as exc:
                msg = f"failed to remove stale pid lock {pid_file}: {exc}"
                raise SystemExit(msg) from exc
            # After successful unlink, retry the lock acquisition
            continue
        else:
            return


def _release_pid_lock(pid_file: Path) -> None:
    try:
        if pid_file.exists():
            current = pid_file.read_text(encoding="utf-8").strip()
            if current == str(os.getpid()):
                pid_file.unlink()
    except OSError:
        logging.getLogger("bot.cli").exception("failed to release pid lock %s", pid_file)


def _setup_signal_handlers(bot: SignalBot) -> None:
    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        try:
            bot.request_shutdown()
        except DEFENSIVE_EXC:
            logging.getLogger("bot.cli").exception("failed to request shutdown")

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except (NotImplementedError, TypeError) as exc:
            logging.getLogger("bot.cli").debug(
                "asyncio signal handler unavailable (%s), using signal.signal",
                repr(exc),
            )
            try:
                signal.signal(sig, lambda _signum, _frame: _request_shutdown())
            except (ValueError, NotImplementedError, OSError) as exc2:
                logging.getLogger("bot.cli").debug(
                    "signal.signal unavailable for %s (%s)", sig, repr(exc2)
                )
        except DEFENSIVE_EXC:
            logging.getLogger("bot.cli").exception("signal handler setup failed")


async def _main(
    *,
    config_path: str | Path = "config.toml",
    harvest_minutes: float = 0.0,
    harvest_symbols: tuple[str, ...] | None = None,
) -> None:
    _bootstrap_env_if_missing()
    settings = load_settings(config_path)
    if harvest_minutes > 0 or harvest_symbols is not None:
        settings = activate_research_harvest(settings, symbols=harvest_symbols)
    elif os.getenv("BOT_RESEARCH_HARVEST", "").strip().lower() in ("1", "true", "yes"):
        settings = activate_research_harvest(settings)
    elif settings.research_harvest.enabled:
        settings = apply_research_harvest_profile(settings)
    if settings.config_path.name != "config.toml":
        sys.stderr.write(f"[INFO] config.toml not found; using {settings.config_path}\n")
    use_telegram = (
        settings.notifiers.provider == "telegram" and not settings.research_harvest.enabled
    )
    if settings.research_harvest.enabled:
        sys.stderr.write(
            "[INFO] research_harvest mode: Telegram delivery disabled; writing capture JSONL\n"
        )
    elif not use_telegram:
        sys.stderr.write(
            "[INFO] notifier provider is not telegram; signal delivery runs in local/log mode\n"
        )
    settings.validate_for_runtime(require_telegram=use_telegram)

    try:
        await generate_and_send_startup_report(
            Path.cwd(),
            send_telegram=use_telegram,
            config_path=str(settings.config_path),
        )
    except DEFENSIVE_EXC as exc:
        sys.stderr.write(f"[ERROR] startup report failed (non-fatal): {exc}\n")

    debug_mode = os.getenv("DEBUG_BOT", "0") in ("1", "true", "yes")
    configure_logging(settings, debug_mode=debug_mode)
    _cleanup_runtime_artifacts(settings)

    # Capture all warnings as log entries
    logging.captureWarnings(capture=True)

    # Redirect stderr to logger while preserving original output
    _orig_stderr = sys.stderr

    class _StderrToLog:
        def __init__(self, logger_name: str, orig: Any) -> None:
            self.logger = logging.getLogger(logger_name)
            self._orig = orig
            self._buf = ""

        def write(self, msg: str) -> None:
            self._orig.write(msg)  # Always write to original stderr
            self._orig.flush()
            # Also log non-empty lines
            self._buf += msg
            if "\n" in self._buf:
                lines = self._buf.split("\n")
                for line in lines[:-1]:
                    if line.strip() and not _is_preformatted_log_stderr(line):
                        self.logger.error("STDERR: %s", line)
                self._buf = lines[-1]

        def flush(self) -> None:
            self._orig.flush()
            if self._buf.strip():
                self.logger.error("STDERR: %s", self._buf)
                self._buf = ""

    sys.stderr = _StderrToLog("stderr", _orig_stderr)

    # Hook to log unhandled exceptions
    def _log_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        del loop
        msg = context.get("exception", context["message"])
        msg_text = str(msg)
        if "Unclosed client session" in msg_text or "Unclosed connector" in msg_text:
            logging.getLogger("asyncio").warning("aiohttp resource cleanup | %s", msg_text)
            return
        logging.getLogger("asyncio").exception(
            "Unhandled exception: %s",
            msg,
            exc_info=msg if isinstance(msg, BaseException) else None,
        )

    asyncio.get_running_loop().set_exception_handler(_log_exception)

    _run_doctor_check(settings)
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}"
    telemetry = TelemetryStore(settings.telemetry_dir, run_id=run_id)
    bot = SignalBot(settings, telemetry=telemetry)
    summary_task: asyncio.Task[None] | None = None
    await _acquire_pid_lock(settings.pid_file)
    try:
        _setup_signal_handlers(bot)
        summary_task = asyncio.create_task(
            run_daily_summary_loop(
                Path.cwd(),
                stop_event=bot._shutdown,  # internal runtime stop event
                config_path=str(settings.config_path),
                send_telegram=use_telegram,
            ),
            name="daily_summary",
        )
        await bot.start()
        runner = asyncio.create_task(bot.run_forever(), name="bot_run_forever")
        if harvest_minutes > 0:
            try:
                await asyncio.wait_for(asyncio.shield(runner), timeout=harvest_minutes * 60.0)
            except TimeoutError:
                logging.getLogger("bot.cli").info(
                    "research_harvest duration reached | minutes=%.1f",
                    harvest_minutes,
                )
                bot._shutdown.set()
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(runner, timeout=120.0)
        else:
            await runner
    finally:
        if summary_task is not None:
            summary_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await summary_task
        try:
            await bot.close()
        finally:
            _release_pid_lock(settings.pid_file)


_CONFIG_DEFAULT = Path("config.toml")


def _config_parent_parser() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--config",
        type=Path,
        default=_CONFIG_DEFAULT,
        help="Path to config.toml (default: config.toml)",
    )
    return parent


def _resolve_config_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "config", _CONFIG_DEFAULT))


def build_parser() -> argparse.ArgumentParser:
    config_parent = _config_parent_parser()
    parser = argparse.ArgumentParser(prog="crypto-signal-bot")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Full DEBUG logs, asyncio loop debug, tracemalloc (or set DEBUG_BOT=1)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_CONFIG_DEFAULT,
        help="Path to config.toml (default: config.toml)",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", parents=[config_parent], help="Run live bot runtime (default)")
    harvest = sub.add_parser(
        "harvest",
        parents=[config_parent],
        help="Research harvest: ~10 symbols, full telemetry, no Telegram (calibration later)",
    )
    harvest.add_argument(
        "--minutes",
        type=float,
        default=60.0,
        help="Run duration in minutes (0 = until Ctrl+C)",
    )
    harvest.add_argument(
        "--symbols",
        nargs="*",
        default=(),
        help="Override harvest symbol list (default: 10 anchors from research_harvest)",
    )
    sub.add_parser("status", parents=[config_parent], help="Show runtime/db status")
    sub.add_parser("stop", parents=[config_parent], help="Stop running bot by pid-file")
    outcomes = sub.add_parser(
        "outcomes",
        help="Compute signal outcome stats from SQLite history (primary)",
    )
    outcomes.add_argument("--days", type=int, default=30)
    outcomes.add_argument("--setup", type=str, default="")
    backtest = sub.add_parser(
        "backtest",
        help="Historical walk-forward validation (Binance klines, no Telegram)",
    )
    backtest.add_argument("--symbol", type=str, default="BTCUSDT")
    backtest.add_argument("--days", type=int, default=7)
    backtest.add_argument("--setup", type=str, default="")
    backtest.add_argument("--interval", type=str, default="15m")
    export = sub.add_parser("export", help="Export outcomes to CSV or Parquet (п.32)")
    export.add_argument("--format", choices=["csv", "parquet"], default="csv")
    export.add_argument("--days", type=int, default=90, help="How many days back to export")
    export.add_argument("--out", type=str, default="", help="Output file path (default: auto)")
    replay = sub.add_parser("replay", help="Show latest replay telemetry rows")
    replay.add_argument("--tail", type=int, default=20)
    db = sub.add_parser("db", help="DB maintenance")
    db_sub = db.add_subparsers(dest="db_command")
    db_sub.add_parser("migrate", parents=[config_parent], help="Apply forward migrations")
    db_clean = db_sub.add_parser("clean", help="Cleanup old outcomes by retention window")
    db_clean.add_argument("--days", type=int, default=30)
    return parser


def run() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config_path = _resolve_config_path(args)

    if getattr(args, "debug", False):
        os.environ["DEBUG_BOT"] = "1"

    if args.command in (None, "run"):
        _run_runtime(config_path)
        return
    if args.command == "harvest":
        symbols = tuple(str(s).strip().upper() for s in (args.symbols or ()) if str(s).strip())
        _run_harvest_runtime(
            config_path,
            minutes=max(0.0, float(args.minutes)),
            symbols=symbols or None,
        )
        return
    if args.command == "status":
        _run_status_command(config_path)
        return
    if args.command == "stop":
        _run_stop_command(config_path)
        return
    if args.command == "outcomes":
        _run_outcomes_command(days=max(1, int(args.days)), setup_id=str(args.setup or "").strip())
        return
    if args.command == "backtest":
        _run_backtest_command(
            symbol=str(args.symbol or "BTCUSDT").strip().upper(),
            days=max(3, int(args.days)),
            setup_id=str(args.setup or "").strip(),
            interval=str(args.interval or "15m").strip().lower(),
            config_path=config_path,
        )
        return
    if args.command == "export":
        asyncio.run(
            _run_export_command(
                days=max(1, int(args.days)),
                fmt=str(args.format or "csv"),
                out_path=str(args.out or "").strip(),
            )
        )
        return
    if args.command == "replay":
        _run_replay_command(tail=max(1, int(args.tail)))
        return
    if args.command == "db":
        db_command = getattr(args, "db_command", None)
        if db_command == "migrate":
            asyncio.run(_db_migrate_command(config_path))
            return
        if db_command == "clean":
            asyncio.run(_db_clean_command(days=max(1, int(args.days))))
            return
        parser.error("db command is required: migrate|clean")


def _run_runtime(config_path: str | Path = _CONFIG_DEFAULT) -> None:
    _run_bot_async(config_path=config_path, harvest_minutes=0.0, harvest_symbols=None)


def _run_harvest_runtime(
    config_path: str | Path,
    *,
    minutes: float,
    symbols: tuple[str, ...] | None,
) -> None:
    os.environ.setdefault("BOT_NOTIFIER_PROVIDER", "none")
    os.environ.setdefault("BOT_DISABLE_HTTP_SERVERS", "1")
    _run_bot_async(
        config_path=config_path,
        harvest_minutes=minutes,
        harvest_symbols=symbols,
    )


def _run_bot_async(
    *,
    config_path: str | Path,
    harvest_minutes: float,
    harvest_symbols: tuple[str, ...] | None,
) -> None:
    _configure_stdio_for_unicode()
    debug_mode = os.getenv("DEBUG_BOT", "0") in ("1", "true", "yes")

    if debug_mode:
        tracemalloc.start(25)
        sys.stderr.write("[DEBUG] tracemalloc enabled | logging level=DEBUG\n")

    if sys.platform == "win32":
        policy_factory = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if policy_factory is not None:
            asyncio.set_event_loop_policy(policy_factory())
    try:
        asyncio.run(
            _main(
                config_path=config_path,
                harvest_minutes=harvest_minutes,
                harvest_symbols=harvest_symbols,
            )
        )
    except KeyboardInterrupt:
        logging.getLogger("bot.cli").info("bot stopped by user")
    finally:
        if debug_mode:
            current, peak = tracemalloc.get_traced_memory()
            logging.getLogger("bot.cli").debug(
                "Memory: current=%.2fMB peak=%.2fMB",
                current / 1024 / 1024,
                peak / 1024 / 1024,
            )


def _run_status_command(config_path: str | Path = _CONFIG_DEFAULT) -> None:
    settings = load_settings(config_path)
    pid = _read_pid_value(settings.pid_file)
    running = bool(pid and _pid_is_alive(pid))
    totals = {"outcomes": 0, "active_signals": 0}
    if settings.db_path.exists():
        with sqlite3.connect(settings.db_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM signal_outcomes")
            totals["outcomes"] = int(cursor.fetchone()[0] or 0)
            cursor = conn.execute(
                "SELECT COUNT(*) FROM active_signals WHERE status IN ('pending','active')"
            )
            totals["active_signals"] = int(cursor.fetchone()[0] or 0)
    print(
        json.dumps(
            {
                "running": running,
                "pid": pid if running else None,
                "pid_file": str(settings.pid_file),
                "db_path": str(settings.db_path),
                "totals": totals,
            },
            ensure_ascii=True,
            indent=2,
        )
    )


def _run_stop_command(config_path: str | Path = _CONFIG_DEFAULT) -> None:
    settings = load_settings(config_path)
    pid = _read_pid_value(settings.pid_file)
    if not pid:
        print("No pid found.")
        return
    if not _pid_is_alive(pid):
        print(f"Process {pid} is not running.")
        return
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (
                completed.stderr or completed.stdout or ""
            ).strip() or f"exit={completed.returncode}"
            msg = f"failed to stop pid {pid} via taskkill: {detail}"
            raise SystemExit(msg)
        print(f"Stopped pid {pid} via taskkill.")
        return
    os.kill(pid, signal.SIGTERM)
    print(f"Sent SIGTERM to pid {pid}.")


def _run_backtest_command(
    *,
    symbol: str,
    days: int,
    setup_id: str,
    interval: str,
    config_path: str | Path,
) -> None:
    from bot.engine.backtest import run_historical_backtest

    settings = load_settings(config_path)

    async def _run() -> dict[str, Any]:
        return await run_historical_backtest(
            settings,
            symbol=symbol,
            days=days,
            setup_id=setup_id,
            interval=interval,
            config_path=str(config_path),
        )

    try:
        report = asyncio.run(_run())
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, ensure_ascii=True, indent=2))


def _run_outcomes_command(*, days: int, setup_id: str = "") -> None:
    settings = load_settings("config.toml")
    if not settings.db_path.exists():
        msg = f"db not found: {settings.db_path}"
        raise SystemExit(msg)
    query = """
        SELECT setup_id, COUNT(*) AS total,
               SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
               AVG(COALESCE(pnl_pct, 0)) AS avg_pnl_pct
        FROM signal_outcomes
        WHERE COALESCE(closed_at, created_at) >= datetime('now', ?)
    """
    params: list[Any] = [f"-{days} days"]
    if setup_id:
        query += " AND setup_id = ?"
        params.append(setup_id)
    query += " GROUP BY setup_id ORDER BY total DESC, setup_id ASC"
    with sqlite3.connect(settings.db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    payload = []
    for setup, total, wins, avg_pnl in rows:
        total_i = int(total or 0)
        wins_i = int(wins or 0)
        payload.append(
            {
                "setup_id": str(setup),
                "total": total_i,
                "wins": wins_i,
                "win_rate": round((wins_i / total_i), 4) if total_i else 0.0,
                "avg_pnl_pct": round(float(avg_pnl or 0.0), 4),
            }
        )
    print(
        json.dumps(
            {"window_days": days, "setup_filter": setup_id or None, "results": payload},
            ensure_ascii=True,
            indent=2,
        )
    )


def _run_replay_command(*, tail: int) -> None:
    settings = load_settings("config.toml")
    replay_dir = settings.telemetry_dir / "replay"
    if not replay_dir.exists():
        msg = f"replay dir not found: {replay_dir}"
        raise SystemExit(msg)
    rows: list[str] = []
    for path in sorted(replay_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        data = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in reversed([ln for ln in data if ln.strip()]):
            rows.append(line)
            if len(rows) >= tail:
                break
        if len(rows) >= tail:
            break
    for row in reversed(rows[-tail:]):
        print(row)


async def _db_migrate_command(config_path: str | Path = _CONFIG_DEFAULT) -> None:
    settings = load_settings(config_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as conn:
        applied = await migrate_db(conn)
    print(
        json.dumps(
            {"db_path": str(settings.db_path), "migrations_applied": applied},
            ensure_ascii=True,
        )
    )


async def _run_export_command(*, days: int, fmt: str, out_path: str) -> None:
    """Export signal_outcomes to CSV or Parquet (п.32)."""
    import polars as pl

    settings = load_settings("config.toml")
    repo = MemoryRepository(settings.db_path, data_dir=settings.data_dir)
    await repo.initialize()
    try:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = await repo._conn.execute_fetchall(
            "SELECT * FROM signal_outcomes WHERE entry_time >= ? ORDER BY entry_time",
            (cutoff,),
        )
        cols = [d[0] for d in (await repo._conn.execute("SELECT * FROM signal_outcomes LIMIT 0")).description or []]
        if not cols:
            # fallback: fetch column names from pragma
            pragma = await repo._conn.execute_fetchall("PRAGMA table_info(signal_outcomes)")
            cols = [r[1] for r in pragma]
        frame = pl.DataFrame(rows, schema=cols, orient="row") if rows else pl.DataFrame(schema=cols)
    finally:
        await repo.close()

    if not out_path:
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        out_path = f"outcomes_{ts}.{fmt}"
    dest = Path(out_path)
    if fmt == "parquet":
        frame.write_parquet(dest)
    else:
        frame.write_csv(dest)
    print(json.dumps({"exported": frame.height, "columns": len(frame.columns), "path": str(dest)}))


async def _db_clean_command(*, days: int) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    settings = load_settings("config.toml")
    repo = MemoryRepository(settings.db_path, data_dir=settings.data_dir)
    await repo.initialize()
    deleted = await repo.cleanup_signal_outcomes_before(cutoff.isoformat())
    await repo.close()
    print(
        json.dumps(
            {
                "db_path": str(settings.db_path),
                "days": days,
                "deleted_outcomes": deleted,
            },
            ensure_ascii=True,
        )
    )

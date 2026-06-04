#!/usr/bin/env python3
"""Keep main.py run alive and log periodic health/signal snapshots."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    from scripts.common import bootstrap_repo_path
except ModuleNotFoundError:
    from common import bootstrap_repo_path

bootstrap_repo_path()

from bot.domain.config import load_settings
from bot.ops.pid_utils import pid_is_alive

LOG = logging.getLogger("scripts.agent_bot_supervisor")
POLL_SECONDS = 120.0
MAX_RUNTIME_SECONDS = 6 * 3600.0


def _runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    if os.environ.get("BOT_ENABLE_DASHBOARD", "1").strip().lower() not in {"0", "false", "no"}:
        env.pop("BOT_DISABLE_HTTP_SERVERS", None)
    else:
        env["BOT_DISABLE_HTTP_SERVERS"] = "1"
    env.pop("BOT_NOTIFIER_PROVIDER", None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _start_bot(*, config_path: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "main.py", "run", "--config", str(config_path)],
        cwd=Path.cwd(),
        env=_runtime_env(),
        stdout=open("logs/agent_live_run.log", "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )


def _stop_bot(settings: object, *, config_path: Path) -> None:
    pid_file = Path(getattr(settings, "pid_file", "bot.pid"))
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except ValueError:
            pid = None
        if pid is not None and pid_is_alive(pid):
            os.kill(pid, signal.SIGTERM)
            time.sleep(5)
            if pid_is_alive(pid):
                os.kill(pid, signal.SIGKILL)
        pid_file.unlink(missing_ok=True)
    subprocess.run(
        [sys.executable, "main.py", "stop", "--config", str(config_path)],
        cwd=Path.cwd(),
        env=_runtime_env(),
        capture_output=True,
        check=False,
    )


def _log_calibration_hook_note(*, config_path: Path) -> None:
    LOG.info(
        "post-run calibration hook | make nightly-calibration "
        "or: python scripts/nightly_strategy_calibration.py --config %s",
        config_path,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument(
        "--calibration-note",
        action="store_true",
        help="Log a post-run reminder to run nightly strategy calibration",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    config_path = Path(args.config)
    settings = load_settings(config_path)
    log_path = Path("logs/supervisor_snapshots.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    proc: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    restarts = 0

    LOG.info(
        "supervisor start | config=%s provider=%s poll=%ss max_hours=6",
        config_path,
        settings.notifiers.provider,
        int(POLL_SECONDS),
    )

    try:
        while time.monotonic() - started < MAX_RUNTIME_SECONDS:
            pid_file = Path(settings.pid_file)
            bot_pid: int | None = None
            if pid_file.exists():
                try:
                    bot_pid = int(pid_file.read_text().strip())
                except ValueError:
                    bot_pid = None
            alive = bool(bot_pid and pid_is_alive(bot_pid))
            if not alive:
                if proc is not None and proc.poll() is not None:
                    LOG.warning("bot exited code=%s", proc.returncode)
                else:
                    LOG.warning("bot not alive (pid=%s)", bot_pid)
                _stop_bot(settings, config_path=config_path)
                proc = None
                proc = _start_bot(config_path=config_path)
                restarts += 1
                LOG.info("bot (re)started | child_pid=%s restarts=%d", proc.pid, restarts)
                time.sleep(60)
                if pid_file.exists():
                    try:
                        alive = pid_is_alive(int(pid_file.read_text().strip()))
                    except ValueError:
                        alive = False
                else:
                    alive = False
                if not alive:
                    LOG.error("bot failed to acquire pid lock after restart")

            snapshot = subprocess.run(
                [sys.executable, "scripts/agent_live_monitor.py"],
                capture_output=True,
                text=True,
                check=False,
            )
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n=== {stamp} alive={alive} ===\n")
                handle.write(snapshot.stdout or "")
                if snapshot.stderr:
                    handle.write(snapshot.stderr)

            if not alive and proc.poll() is None:
                LOG.error("pid file stale but subprocess running — waiting")
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        LOG.info("supervisor interrupted")
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
        _stop_bot(settings, config_path=config_path)

    LOG.info("supervisor done | restarts=%d", restarts)
    if args.calibration_note:
        _log_calibration_hook_note(config_path=config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

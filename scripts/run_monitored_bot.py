#!/usr/bin/env python3
"""Run main.py run for up to 6h with auto-restart and periodic snapshots."""

from __future__ import annotations

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
from bot.ops.pid_utils import acquire_pid_lock, pid_is_alive, release_pid_lock

LOG = logging.getLogger("scripts.run_monitored_bot")
MAX_SECONDS = 6 * 3600
POLL_SECONDS = 90.0
MONITOR_LOCK = Path("data/bot/monitor.lock")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    if os.environ.get("BOT_ENABLE_DASHBOARD", "1").strip().lower() not in {"0", "false", "no"}:
        env.pop("BOT_DISABLE_HTTP_SERVERS", None)
    else:
        env["BOT_DISABLE_HTTP_SERVERS"] = "1"
    env.pop("BOT_NOTIFIER_PROVIDER", None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _bot_alive(settings: object) -> bool:
    pid_file = Path(getattr(settings, "pid_file", "bot.pid"))
    if not pid_file.exists():
        return False
    try:
        return pid_is_alive(int(pid_file.read_text().strip()))
    except (ValueError, OSError):
        return False


def _stop(settings: object) -> None:
    subprocess.run(
        [sys.executable, "main.py", "stop"],
        cwd=Path.cwd(),
        env=_env(),
        capture_output=True,
        check=False,
    )
    Path(getattr(settings, "pid_file", "bot.pid")).unlink(missing_ok=True)


def _start() -> subprocess.Popen[bytes]:
    log = open("logs/agent_live_run.log", "a", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "main.py", "run"],
        cwd=Path.cwd(),
        env=_env(),
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/agent_supervisor.log", encoding="utf-8"),
        ],
    )
    try:
        acquire_pid_lock(MONITOR_LOCK)
    except SystemExit as exc:
        LOG.error("%s", exc)
        return 1

    settings = load_settings("config.toml")
    Path("logs").mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    restarts = 0
    proc: subprocess.Popen[bytes] | None = None

    LOG.info("monitored bot session | provider=%s lock=%s", settings.notifiers.provider, MONITOR_LOCK)

    try:
        while time.monotonic() - started < MAX_SECONDS:
            if not _bot_alive(settings):
                if proc is not None and proc.poll() is not None:
                    LOG.warning("bot exit code=%s", proc.returncode)
                _stop(settings)
                proc = _start()
                restarts += 1
                LOG.info("bot started restarts=%d wrapper_pid=%s", restarts, proc.pid)
                time.sleep(75)
            subprocess.run(
                [sys.executable, "scripts/agent_live_monitor.py"],
                cwd=Path.cwd(),
                check=False,
            )
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        LOG.info("interrupted")
    finally:
        _stop(settings)
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
        release_pid_lock(MONITOR_LOCK)

    LOG.info("monitored session done | restarts=%d", restarts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

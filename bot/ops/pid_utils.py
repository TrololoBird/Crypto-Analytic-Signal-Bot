"""Shared PID lock helpers for cli and live supervision scripts."""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path


def pid_is_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` is running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = cast("Any", ctypes).windll.kernel32
        inherit_handles = False
        handle = kernel32.OpenProcess(process_query_limited_information, inherit_handles, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # ERROR_ACCESS_DENIED (5): process exists but we cannot query it.
        return int(kernel32.GetLastError()) == 5
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid_file(pid_file: Path) -> int:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def clear_stale_pid_file(pid_file: Path) -> bool:
    """Remove pid file when holder is not alive. Returns True if removed."""
    if not pid_file.exists():
        return False
    holder = read_pid_file(pid_file)
    if holder > 0 and pid_is_alive(holder):
        return False
    try:
        pid_file.unlink()
    except OSError:
        return False
    return True


def acquire_pid_lock(pid_file: Path, *, owner_pid: int | None = None) -> None:
    """Acquire an exclusive PID lock file or raise ``SystemExit`` if held."""
    owner = owner_pid if owner_pid is not None else os.getpid()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    retries = 0
    while True:
        try:
            fd = os.open(str(pid_file), flags)
            try:
                os.write(fd, str(owner).encode("ascii", errors="strict"))
            finally:
                os.close(fd)
        except FileExistsError:
            existing_pid = read_pid_file(pid_file)
            if existing_pid and existing_pid != owner and pid_is_alive(existing_pid):
                msg = (
                    f"another process is already running with pid {existing_pid} (lock={pid_file})"
                )
                raise SystemExit(msg) from None
            if existing_pid == 0:
                retries += 1
                try:
                    age_s = max(0.0, time.time() - pid_file.stat().st_mtime)
                except OSError:
                    age_s = 0.0
                if retries <= 50 or age_s < 10.0:
                    time.sleep(0.1)
                    continue
            try:
                pid_file.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                msg = f"failed to remove stale pid lock {pid_file}: {exc}"
                raise SystemExit(msg) from exc
        else:
            return


def release_pid_lock(pid_file: Path, *, owner_pid: int | None = None) -> None:
    """Release PID lock when owned by ``owner_pid`` (default: current process)."""
    owner = owner_pid if owner_pid is not None else os.getpid()
    try:
        if pid_file.exists() and read_pid_file(pid_file) == owner:
            pid_file.unlink()
    except OSError:
        return


def find_bot_main_pids(repo_root: Path) -> list[int]:
    """Find python.exe processes running this repo's main.py."""
    root = str(repo_root.resolve()).replace("'", "''")
    where_clause = (
        f"Where-Object {{ $_.CommandLine -like '*{root}*' "
        f"-and $_.CommandLine -like '*main.py*' }} | "
    )
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        f"{where_clause}"
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", script],
            text=True,
            cwd=str(repo_root),
            timeout=30,
        )
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if stripped.isdigit():
            pids.append(int(stripped))
    return pids


def stop_bot_processes(
    *,
    repo_root: Path,
    pid_file: Path,
    exclude_pids: set[int] | None = None,
) -> list[int]:
    """Terminate bot main.py processes and remove pid lock. Returns stopped PIDs."""
    exclude = exclude_pids or set()
    targets: set[int] = set(find_bot_main_pids(repo_root))
    if pid_file.exists():
        targets.add(read_pid_file(pid_file))
    stopped: list[int] = []
    for pid in sorted(targets):
        if pid <= 0 or pid in exclude or not pid_is_alive(pid):
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=30,
            )
            stopped.append(pid)
        except OSError:
            continue
    clear_stale_pid_file(pid_file)
    return stopped

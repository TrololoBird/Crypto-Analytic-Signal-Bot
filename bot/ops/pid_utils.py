"""Shared PID lock helpers for cli and live supervision scripts."""

from __future__ import annotations

import ctypes
import os
import subprocess
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

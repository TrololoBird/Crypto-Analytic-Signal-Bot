#!/usr/bin/env python3
"""Spawn a fully detached child process (survives closing the parent shell)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command after --")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    args = parser.parse_args()
    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("usage: launch_detached.py --log PATH --pid-file PATH -- cmd...", file=sys.stderr)
        return 2

    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_handle = args.log.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(args.cwd),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    args.pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
    print(proc.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

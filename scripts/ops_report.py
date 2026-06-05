#!/usr/bin/env python3
"""Ops reports - startup summary and daily digest (v9 scripts entry)."""

from __future__ import annotations

from bot.ops.startup_report import generate_and_send_startup_report, run_daily_summary_loop

__all__ = ["generate_and_send_startup_report", "run_daily_summary_loop"]


if __name__ == "__main__":
    import asyncio

    asyncio.run(generate_and_send_startup_report())

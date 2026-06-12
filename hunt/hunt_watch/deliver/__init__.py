"""Hunt delivery layer — sniper gate + Telegram formatting (split from watch.py)."""

from hunt_watch.deliver.sniper import SniperConfig, sniper_block_reason
from hunt_watch.deliver.telegram import (
    format_entry_telegram,
    format_followup_telegram,
    format_setup_lines,
    send_telegram_chunks,
    split_telegram,
)

__all__ = [
    "SniperConfig",
    "format_entry_telegram",
    "format_followup_telegram",
    "format_setup_lines",
    "send_telegram_chunks",
    "sniper_block_reason",
    "split_telegram",
]

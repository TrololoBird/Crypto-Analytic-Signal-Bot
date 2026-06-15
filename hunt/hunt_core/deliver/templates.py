"""Table-driven Telegram templates (§K / §8D)."""
from __future__ import annotations

import html
from typing import Any


def format_confirm_strong(
    row: dict[str, Any],
    *,
    direction: str,
    delivery_tier: str = "triggered",
    confirm_reasons: list[str] | None = None,
) -> str:
    """Confirm TG body — delegates to unified delivery card formatter."""
    from hunt_core.deliver.dispatch import format_delivery_card

    setup = row.get("dump") if direction == "short" else row.get("long")
    setup = setup if isinstance(setup, dict) else {}
    return format_delivery_card(
        row,
        direction=direction,
        setup=setup,
        delivery_tier=delivery_tier,
        confirm_reasons=confirm_reasons,
    )


def format_advisory_early(row: dict[str, Any], *, note: str) -> str:
    sym = str(row.get("symbol") or "").replace("USDT", "-USDT")
    return f"⏳ <b>{sym}</b> · EARLY advisory\n{note}"


def format_pinned_summary(row: dict[str, Any]) -> str:
    sym = str(row.get("symbol") or "").replace("USDT", "-USDT")
    verdict = row.get("pinned_verdict") or row.get("pinned_scenario") or {}
    direction = verdict.get("direction") or verdict.get("primary_direction") or "—"
    return f"📌 <b>{sym}</b> · {direction}"


def format_telegram_confirm(
    row: dict[str, Any],
    *,
    direction: str,
    confirm_reasons: list[str],
    delivery_tier: str = "triggered",
) -> str:
    """Closed-bar confirm card + optional confluence grid."""
    from hunt_core.analysis.confluence_grid import build_confluence_grid, format_grid_telegram

    setup = row["dump"] if direction == "short" else row["long"]
    body = format_confirm_strong(
        {**row, "dump" if direction == "short" else "long": setup},
        direction=direction,
        delivery_tier=delivery_tier,
        confirm_reasons=confirm_reasons,
    )
    grid = build_confluence_grid(row)
    if grid:
        body = f"{body}\n{format_grid_telegram(grid)}"
    if confirm_reasons:
        body = f"{body}\n<i>{html.escape(', '.join(confirm_reasons[:6]))}</i>"
    return body


def format_squeeze_telegram(row: dict[str, Any]) -> str:
    from hunt_core.deliver.telegram import format_squeeze_telegram as _fmt

    return _fmt(row)


def format_followup_telegram_message(followup: Any, row: dict[str, Any]) -> str:
    from hunt_core.deliver.telegram import format_followup_telegram as _fmt

    return _fmt(followup, row)


__all__ = [
    "format_advisory_early",
    "format_confirm_strong",
    "format_followup_telegram_message",
    "format_pinned_summary",
    "format_squeeze_telegram",
    "format_telegram_confirm",
]

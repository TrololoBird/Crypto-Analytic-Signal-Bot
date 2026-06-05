"""Startup tracking summary for operator Telegram digest."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def format_startup_tracking_digest(summary: Mapping[str, Any]) -> str:
    pending = int(summary.get("pending") or 0)
    active = int(summary.get("active") or 0)
    repaired = int(summary.get("repaired") or 0)
    review_closed = int(summary.get("review_closed") or 0)
    stale_expired = int(summary.get("stale_expired") or 0)
    lines = [
        "<b>Startup tracking</b>",
        f"Pending: <code>{pending}</code> · Active: <code>{active}</code>",
    ]
    if repaired:
        lines.append(f"Repaired zone-touch → active: <code>{repaired}</code>")
    if review_closed:
        lines.append(f"Review sweep closed: <code>{review_closed}</code>")
    if stale_expired:
        lines.append(f"Stale open signals expired: <code>{stale_expired}</code>")
    if not repaired and not review_closed and not stale_expired:
        lines.append("<i>Без миграций — состояние чистое</i>")
    return "\n".join(lines)

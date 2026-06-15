"""Table-driven Telegram templates (§K / §8D)."""
from __future__ import annotations

from typing import Any


def format_confirm_strong(row: dict[str, Any], *, direction: str) -> str:
    sym = str(row.get("symbol") or "").replace("USDT", "-USDT")
    setup = row.get("dump") if direction == "short" else row.get("long")
    setup = setup if isinstance(setup, dict) else {}
    fuel = setup.get("dump_score") if direction == "short" else setup.get("long_score")
    phase = setup.get("phase") or (row.get("lifecycle") or {}).get("phase")
    dir_ru = "ШОРТ" if direction == "short" else "ЛОНГ"
    lines = [
        f"✅ <b>{sym}</b> · {dir_ru} · CONFIRM",
        f"Phase: <code>{phase}</code> · fuel: <code>{fuel}</code>",
    ]
    for key, label in (("entry_zone", "Entry"), ("stop_loss", "SL"), ("tp1", "TP1"), ("tp2", "TP2")):
        val = setup.get(key)
        if val is not None:
            lines.append(f"{label}: <code>{val}</code>")
    return "\n".join(lines)


def format_advisory_early(row: dict[str, Any], *, note: str) -> str:
    sym = str(row.get("symbol") or "").replace("USDT", "-USDT")
    return f"⏳ <b>{sym}</b> · EARLY advisory\n{note}"


def format_pinned_summary(row: dict[str, Any]) -> str:
    sym = str(row.get("symbol") or "").replace("USDT", "-USDT")
    verdict = row.get("pinned_verdict") or row.get("pinned_scenario") or {}
    direction = verdict.get("direction") or verdict.get("primary_direction") or "—"
    return f"📌 <b>{sym}</b> · {direction}"


__all__ = ["format_advisory_early", "format_confirm_strong", "format_pinned_summary"]

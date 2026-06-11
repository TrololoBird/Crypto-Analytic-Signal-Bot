"""Compare hunt bot ticks vs independent Binance REST analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from hunt_watch.paths import SNAPSHOTS, TICK_JSONL, WATCHLIST

Verdict = Literal[
    "agree",
    "bot_short_premature",
    "bot_long_risky",
    "bot_phase_mismatch",
    "bot_no_setup",
    "independent_error",
    "no_bot_tick",
]


@dataclass(frozen=True, slots=True)
class DiffRow:
    symbol: str
    verdict: Verdict
    bot_phase: str | None
    bot_bias: str | None
    bot_short: dict[str, Any] | None
    bot_long: dict[str, Any] | None
    ind_bias: str | None
    ind_score_short: int | None
    ind_score_long: int | None
    note: str
    bot_ts: str | None = None
    bot_price: float | None = None
    ind_price: float | None = None


def load_latest_bot_ticks(
    path: Path = TICK_JSONL,
    *,
    symbols: set[str] | None = None,
    max_age_minutes: float = 15.0,
) -> dict[str, dict[str, Any]]:
    """Last *fresh* jsonl row per symbol — stale rows (paused bot) must not be
    compared against live market data (pause-deadlock bug)."""
    if not path.exists():
        return {}
    latest: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper()
            if not sym or row.get("error"):
                continue
            if symbols is not None and sym not in symbols:
                continue
            latest[sym] = row
    if max_age_minutes > 0:
        cutoff = datetime.now(UTC) - timedelta(minutes=max_age_minutes)
        fresh: dict[str, dict[str, Any]] = {}
        for sym, row in latest.items():
            try:
                ts = datetime.fromisoformat(str(row.get("ts")))
            except (TypeError, ValueError):
                continue
            if ts >= cutoff:
                fresh[sym] = row
        return fresh
    return latest


def load_watchlist_symbols(path: Path = WATCHLIST) -> list[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return []
    rows = payload.get("watchlist") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        str(row["symbol"]).upper()
        for row in rows
        if isinstance(row, dict) and row.get("symbol")
    ]


def _bot_side(row: dict[str, Any]) -> str:
    lc = row.get("lifecycle") or {}
    bias = str(lc.get("recommended_bias") or "wait")
    phase = str(lc.get("phase") or "")
    dump = row.get("dump") or {}
    long = row.get("long") or {}
    short_conf = bool(dump.get("confirmed"))
    long_conf = bool(long.get("confirmed"))
    short_score = float(dump.get("dump_score") or 0)
    long_score = float(long.get("long_score") or 0)
    if short_conf:
        return "short"
    if long_conf:
        return "long"
    # dump_active + wait = monitoring only (mid-dump cap), not a long call
    if phase == "dump_active" and bias == "wait" and not short_conf and not long_conf:
        return "neutral"
    if not long_conf and long_score < 45 and not short_conf and short_score < 45:
        if short_score > long_score + 5:
            return "neutral"
        if bias == "long":
            return "neutral"
        if bias == "short":
            return "short"
        return "neutral"
    if bias in {"short", "long"}:
        return bias
    if short_score > long_score + 10:
        return "short"
    if long_score > short_score + 10:
        return "long"
    return "neutral"


def _ind_side(ind: dict[str, Any]) -> str:
    bias = str(ind.get("bias") or "")
    if "invalid_short" in bias or bias.startswith("long"):
        return "long"
    if "short_still_valid" in bias:
        return "short"
    return "neutral"


def compare_row(
    bot: dict[str, Any] | None,
    independent: dict[str, Any],
) -> DiffRow:
    sym = str(independent.get("symbol") or "").upper()
    if independent.get("error"):
        return DiffRow(
            symbol=sym,
            verdict="independent_error",
            bot_phase=None,
            bot_bias=None,
            bot_short=None,
            bot_long=None,
            ind_bias=None,
            ind_score_short=None,
            ind_score_long=None,
            note=str(independent.get("error")),
        )
    if bot is None:
        ind = independent.get("independent") or {}
        return DiffRow(
            symbol=sym,
            verdict="no_bot_tick",
            bot_phase=None,
            bot_bias=None,
            bot_short=None,
            bot_long=None,
            ind_bias=str(ind.get("bias")),
            ind_score_short=int(ind.get("score_short") or 0),
            ind_score_long=int(ind.get("score_long") or 0),
            note="no bot jsonl tick",
        )

    lc = bot.get("lifecycle") or {}
    dump = bot.get("dump") or {}
    long = bot.get("long") or {}
    ind = independent.get("independent") or {}
    bot_phase = str(lc.get("phase") or "")
    bot_bias = str(lc.get("recommended_bias") or "wait")
    bot_side = _bot_side(bot)
    ind_side = _ind_side(ind)
    ind_bias = str(ind.get("bias") or "")

    bot_short = {
        "score": dump.get("dump_score"),
        "phase": dump.get("phase"),
        "confirmed": dump.get("confirmed"),
        "rr": dump.get("risk_reward"),
    } if dump else None
    bot_long = {
        "score": long.get("long_score"),
        "phase": long.get("phase"),
        "confirmed": long.get("confirmed"),
        "rr": long.get("risk_reward"),
    } if long else None

    note_parts: list[str] = []
    verdict: Verdict = "agree"

    if bot_side == "short" and ind_side == "long":
        verdict = "bot_short_premature"
        note_parts.append("independent invalidates short fade")
    elif bot_side == "long" and ind_side == "short":
        verdict = "bot_long_risky"
        note_parts.append("independent favors continuation down")
    elif bot_phase == "exhaustion_at_high" and lc.get("invalidate_short"):
        verdict = "bot_phase_mismatch"
        note_parts.append("lifecycle invalidate_short vs exhaustion phase")
    elif bot_phase == "no_setup" and max(
        float(dump.get("dump_score") or 0), float(long.get("long_score") or 0)
    ) < 45:
        verdict = "bot_no_setup"
        note_parts.append("both sides below forming threshold")

    if bool(dump.get("confirmed")) and lc.get("invalidate_short"):
        verdict = "bot_short_premature"
        note_parts.append("confirmed short but lifecycle invalidate_short")
    if bool(long.get("confirmed")) and bot_phase == "dump_active":
        verdict = "bot_long_risky"
        note_parts.append("confirmed long during dump_active")

    if not note_parts:
        note_parts.append("bot and independent aligned")

    return DiffRow(
        symbol=sym,
        verdict=verdict,
        bot_phase=bot_phase,
        bot_bias=bot_bias,
        bot_short=bot_short,
        bot_long=bot_long,
        ind_bias=ind_bias,
        ind_score_short=int(ind.get("score_short") or 0),
        ind_score_long=int(ind.get("score_long") or 0),
        note="; ".join(note_parts),
        bot_ts=str(bot.get("ts") or "")[:19] or None,
        bot_price=float(bot.get("price") or 0) or None,
        ind_price=float(independent.get("price") or 0) or None,
    )


def format_diff_table(rows: list[DiffRow]) -> str:
    lines = [
        f"{'SYMBOL':<12} {'VERDICT':<22} {'BOT_PHASE':<20} {'IND_BIAS':<28} NOTE",
        "-" * 110,
    ]
    for r in rows:
        ind = (r.ind_bias or "")[:27]
        phase = (r.bot_phase or "—")[:19]
        lines.append(
            f"{r.symbol:<12} {r.verdict:<22} {phase:<20} {ind:<28} {r.note[:48]}"
        )
    return "\n".join(lines)


def save_diff_report(
    rows: list[DiffRow],
    *,
    path: Path | None = None,
    meta: dict[str, Any] | None = None,
) -> Path:
    out = path or SNAPSHOTS / "verify_diff.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta or {},
        "rows": [
            {
                "symbol": r.symbol,
                "verdict": r.verdict,
                "bot_phase": r.bot_phase,
                "bot_bias": r.bot_bias,
                "bot_short": r.bot_short,
                "bot_long": r.bot_long,
                "ind_bias": r.ind_bias,
                "ind_score_short": r.ind_score_short,
                "ind_score_long": r.ind_score_long,
                "note": r.note,
                "bot_ts": r.bot_ts,
                "bot_price": r.bot_price,
                "ind_price": r.ind_price,
            }
            for r in rows
        ],
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out

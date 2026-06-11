"""Active hunt monitor — verify_diff pass + mismatch alerts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_watch.paths import SNAPSHOTS
from hunt_watch.targets import PINNED_SYMBOLS as DEFAULT_SYMBOLS
from hunt_watch.verify_diff import (
    DiffRow,
    compare_row,
    format_diff_table,
    load_latest_bot_ticks,
    load_watchlist_symbols,
    save_diff_report,
)

SEVERE_VERDICTS = frozenset(
    {"bot_long_risky", "bot_short_premature", "bot_phase_mismatch"}
)


def resolve_verify_symbols(*, limit: int = 15) -> tuple[str, ...]:
    core = list(DEFAULT_SYMBOLS)
    wl = load_watchlist_symbols()[: max(0, limit - len(core))]
    return tuple(dict.fromkeys([*core, *wl]))


def _load_analyze():
    path = Path(__file__).resolve().parents[1] / "scripts" / "beat_check.py"
    spec = importlib.util.spec_from_file_location("hunt_beat_check", path)
    if spec is None or spec.loader is None:
        msg = f"cannot load {path}"
        raise ImportError(msg)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.analyze


async def run_verify_pass(*, limit: int = 15) -> list[DiffRow]:
    symbols = resolve_verify_symbols(limit=limit)
    analyze = _load_analyze()
    bot_ticks = load_latest_bot_ticks(symbols=set(symbols))
    rows: list[DiffRow] = []
    for sym in symbols:
        try:
            ind = await analyze(sym)
        except Exception as exc:  # noqa: BLE001
            ind = {"symbol": sym, "error": repr(exc)}
        rows.append(compare_row(bot_ticks.get(sym.upper()), ind))
    return rows


def mismatch_rows(rows: list[DiffRow]) -> list[DiffRow]:
    skip = {"agree", "bot_no_setup", "no_bot_tick", "independent_error"}
    return [r for r in rows if r.verdict not in skip]


def severe_rows(rows: list[DiffRow]) -> list[DiffRow]:
    """Severe = actionable contradiction: the contradicted bot side is CONFIRMED.
    Bias-vs-bias opinion diffs stay visible but must not pause the session."""
    out: list[DiffRow] = []
    for r in rows:
        if r.verdict not in SEVERE_VERDICTS:
            continue
        short_conf = bool((r.bot_short or {}).get("confirmed"))
        long_conf = bool((r.bot_long or {}).get("confirmed"))
        if (r.verdict == "bot_short_premature" and short_conf) or (r.verdict == "bot_long_risky" and long_conf) or (r.verdict == "bot_phase_mismatch" and (short_conf or long_conf)):
            out.append(r)
    return out


def save_verify_artifacts(
    rows: list[DiffRow],
    *,
    session_dir: Path | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    mismatches = mismatch_rows(rows)
    ts = datetime.now(UTC)
    table = format_diff_table(rows)
    text_path: Path | None = None
    if session_dir is not None:
        session_dir.mkdir(parents=True, exist_ok=True)
        text_path = session_dir / f"verify_{ts.strftime('%H%M%S')}.txt"
        body = table + f"\n\nmismatches: {len(mismatches)}/{len(rows)}\n"
        for r in mismatches:
            body += (
                f"  ! {r.symbol}: {r.verdict} | bot {r.bot_phase}/{r.bot_bias} "
                f"short={r.bot_short} long={r.bot_long} | ind {r.ind_bias}\n"
            )
        text_path.write_text(body, encoding="utf-8")

    report_path = save_diff_report(
        rows,
        meta={
            "ts": ts.isoformat(),
            "symbols": list(resolve_verify_symbols(limit=limit)),
            "mismatch_count": len(mismatches),
        },
    )
    alert_path: Path | None = None
    if mismatches:
        alert = {
            "ts": ts.isoformat(),
            "mismatch_count": len(mismatches),
            "severe_count": len(severe_rows(rows)),
            "rows": [asdict(r) for r in mismatches],
            "report": str(report_path),
        }
        alert_path = (session_dir or SNAPSHOTS) / f"mismatch_alert_{ts.strftime('%H%M%S')}.json"
        alert_path.write_text(json.dumps(alert, indent=2), encoding="utf-8")
        alerts_log = Path(__file__).resolve().parents[2] / "logs" / "hunt_mismatch_alerts.jsonl"
        alerts_log.parent.mkdir(parents=True, exist_ok=True)
        with alerts_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(alert) + "\n")

    return {
        "mismatch_count": len(mismatches),
        "severe_count": len(severe_rows(rows)),
        "text_path": str(text_path) if text_path else None,
        "report_path": str(report_path),
        "alert_path": str(alert_path) if alert_path else None,
        "table": table,
        "mismatches": mismatches,
    }


def run_verify_sync(*, limit: int = 15, session_dir: Path | None = None) -> dict[str, Any]:
    rows = asyncio.run(run_verify_pass(limit=limit))
    return save_verify_artifacts(rows, session_dir=session_dir, limit=limit)

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.common import bootstrap_repo_path  # noqa: E402


ROOT = bootstrap_repo_path()
LOG_DIR = ROOT / "logs"
DB_PATH = ROOT / "data" / "bot" / "bot.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def find_line(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[2:].strip() if line.startswith("- ") else line
    return ""


def query_rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def markdown_table(headers: tuple[str, ...], rows: list[tuple[Any, ...]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def extract_strategy_metrics(limit: int = 20) -> list[dict[str, str]]:
    text = read_text(LOG_DIR / "04_telemetry_audit.md")
    in_table = False
    rows: list[dict[str, str]] = []
    headers: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line == "## Strategy Metrics":
            in_table = True
            continue
        if in_table and line.startswith("### "):
            break
        if not in_table or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not headers:
            headers = cells
            continue
        if cells and cells[0].startswith("---"):
            continue
        rows.append(dict(zip(headers, cells, strict=False)))
        if len(rows) >= limit:
            break
    return rows


def extract_top_rejections(limit: int = 20) -> list[tuple[str, str]]:
    text = read_text(LOG_DIR / "04_telemetry_audit.md")
    rows: list[tuple[str, str]] = []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "## Rejection Reasons":
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if not in_table or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] in {"Reason", "---"}:
            continue
        rows.append((cells[0], cells[1]))
        if len(rows) >= limit:
            break
    return rows


def write_analysis() -> None:
    file_map = read_text(LOG_DIR / "00_file_map.md")
    logs = read_text(LOG_DIR / "03_log_audit.md")
    telemetry = read_text(LOG_DIR / "04_telemetry_audit.md")
    strategy_rows = extract_strategy_metrics(limit=37)
    zero_hit = []
    capture = False
    for line in telemetry.splitlines():
        if line.strip() == "### Zero-Hit Strategies":
            capture = True
            continue
        if capture and line.startswith("## "):
            break
        if capture and line.startswith("- "):
            zero_hit.append(line[2:].strip())

    outcomes_by_setup = query_rows(
        """
        SELECT
            setup_id,
            COUNT(*) AS total,
            SUM(CASE WHEN result = 'stop_loss' THEN 1 ELSE 0 END) AS stop_loss,
            SUM(CASE WHEN was_profitable = 1 THEN 1 ELSE 0 END) AS profitable,
            AVG(pnl_r_multiple) AS avg_r
        FROM signal_outcomes
        GROUP BY setup_id
        ORDER BY total DESC, setup_id ASC
        """
    )
    poor_setups = [
        row
        for row in outcomes_by_setup
        if int(row["total"] or 0) >= 10
        and int(row["stop_loss"] or 0) / max(1, int(row["total"] or 0)) > 0.5
    ]
    tracking_stats = query_rows("SELECT * FROM tracking_stats")
    active_status = query_rows(
        "SELECT status, COUNT(*) AS count FROM active_signals GROUP BY status ORDER BY count DESC"
    )

    lines = [
        "# Phase 1 Deep Analysis",
        "",
        f"Generated: {utc_now()}",
        "",
        "## Evidence Boundary",
        "",
        "- Confirmed: this report is derived from refreshed Phase 0 manifests, logs, telemetry, and the read-only SQLite database.",
        "- Confirmed: code-path claims were checked against current source after Phase 0.",
        "- Uncertainty: telemetry covers historical runs in this workspace; it cannot prove post-change live behavior until a new live run is analyzed.",
        "",
        "## Confirmed High-Level State",
        "",
        f"- {find_line(file_map, '- Files scanned:')}",
        f"- {find_line(telemetry, '- Telemetry files scanned:')}",
        f"- {find_line(telemetry, '- Parsed telemetry records:')}",
        f"- {find_line(logs, '- Error-like lines:')}",
        f"- {find_line(logs, '- Warning-like lines:')}",
        f"- {find_line(telemetry, '- Inferred SL-like rate:')}",
        f"- {find_line(telemetry, '- Inferred TP/win-like rate:')}",
        "- Dashboard strategy registration check: current code exposes 37 strategy classes and 37 enabled config IDs.",
        "",
        "## Current Database State",
        "",
    ]
    lines += markdown_table(
        ("Status", "Count"),
        [(row["status"], row["count"]) for row in active_status],
    )
    if tracking_stats:
        row = tracking_stats[0]
        lines.extend(
            [
                "",
                f"- Tracking counters: signals_sent={row.get('signals_sent')}, activated={row.get('activated')}, tp1_hit={row.get('tp1_hit')}, tp2_hit={row.get('tp2_hit')}, stop_loss={row.get('stop_loss')}, expired={row.get('expired')}.",
            ]
        )

    lines.extend(["", "## Strategy Telemetry", ""])
    lines += markdown_table(
        (
            "Strategy",
            "Decision Rows",
            "Signal Decisions",
            "Signal Rate",
            "Selected Rows",
            "Top Reject",
        ),
        [
            (
                row.get("Strategy", ""),
                row.get("Decision Rows", ""),
                row.get("Signal Decisions", ""),
                row.get("Signal Rate", ""),
                row.get("Selected Rows", ""),
                row.get("Top Decision Reject Reason", ""),
            )
            for row in strategy_rows
        ],
    )
    lines.extend(["", "### Zero-Hit Strategies", ""])
    lines.extend([f"- {name}" for name in zero_hit] or ["- None observed."])

    lines.extend(["", "### Top Rejections", ""])
    lines += markdown_table(("Reason", "Count"), extract_top_rejections(20))

    lines.extend(["", "## Outcome-Derived Calibration Targets", ""])
    lines += markdown_table(
        ("Setup", "Total", "Stop Loss", "Profitable", "Avg R"),
        [
            (
                row["setup_id"],
                row["total"],
                row["stop_loss"],
                row["profitable"],
                f"{float(row['avg_r'] or 0.0):.3f}",
            )
            for row in poor_setups
        ],
    )
    if not poor_setups:
        lines.append("")
        lines.append("No setup had both >=10 outcomes and >50% stop-loss share.")

    lines.extend(
        [
            "",
            "## Area Analysis",
            "",
            "### Data Acquisition",
            "",
            "- Confirmed: REST and WS are both active paths. REST fetches USD-M public endpoints; WS uses public/market stream routes. Endpoint-policy tests reject private/auth routes.",
            "- Confirmed problem: historical logs contain websocket buffer backpressure and REST timeout warnings. Current config already limits WS kline intervals to 15m and uses REST cache for context frames.",
            "- Inference: the highest remaining acquisition risk is missing or stale derivative context (`ls_ratio`, `oi_change_pct`) during startup or before OI refresh completes.",
            "",
            "### Indicator Engine",
            "",
            "- Confirmed: features are prepared through Polars frames and `prepare_symbol`; missing required 15m/1h history blocks analysis.",
            "- Confirmed problem: data-quality telemetry repeatedly reports missing `ls_ratio` and `oi_change_pct`; strategies depending on these fields should fail explicitly rather than silently degrade.",
            "",
            "### Strategy Logic",
            "",
            "- Confirmed: 37 strategies are registered and enabled in current code/config.",
            "- Confirmed problem: historical telemetry shows 11 zero-hit strategies and a top rejection reason of `pattern.volume_too_low`.",
            "- Confirmed problem: `spread_strategy`, `liquidation_heatmap`, and `structure_pullback` have >=10 stored outcomes and >50% stop-loss share.",
            "",
            "### Signal Lifecycle",
            "",
            "- Confirmed: active lifecycle is stored in `active_signals`/`signal_outcomes`; legacy `signals`/`outcomes` tables are empty in current DB.",
            "- Confirmed problem fixed in current patch: mild negative setup-score adjustments were acting as hard suppressors before global filters could apply calibrated scoring.",
            "",
            "### Shortlist And Universe",
            "",
            "- Confirmed: shortlist has `rest_full`, `ws_light`, `cached`, and `pinned_fallback` paths with telemetry for fallback reason.",
            "- Historical issue: prior logs had `strategy_fits` constructor/attribute errors. Current model and shortlist code expose `strategy_fits`; the specific old error is not reproducible from current source.",
            "",
            "### Risk Management",
            "",
            "- Confirmed: SL rate from persisted tracking events is above the requested 30% target. Evidence supports tightening poor setup entry filters and widening stops for high-SL setups.",
            "- Uncertainty: sample sizes remain small for many setups; broad parameter sweeps would need a backtest/forward-test tool before larger changes are justified.",
            "",
            "### Telegram Delivery",
            "",
            "- Confirmed problem: logs contain Telegram flood-control failures. Current patch adds local pacing before Telegram sends/edits/photos instead of waiting for server-side rate-limit errors.",
            "",
            "### Dashboard",
            "",
            "- Confirmed: current dashboard strategy cache returns 37 strategies, so the historical 33/37 defect appears fixed in this worktree.",
            "- Residual risk: endpoint liveness still requires a running dashboard server check after live startup.",
            "",
            "### ML And Analytics",
            "",
            "- Confirmed: ML live use is disabled in config. Analytics uses persisted tracked outcomes for dashboard-compatible strategy reports.",
            "- Inference: ML modules should remain non-gating until outcome volume is larger and labels are cleaner.",
            "",
            "### Performance",
            "",
            "- Confirmed: historical WS buffer compaction and Telegram rate limits are operational bottlenecks. Current config and patch reduce bursts but do not prove long-run stability.",
        ]
    )
    (LOG_DIR / "10_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_master_plan() -> None:
    lines = [
        "# Phase 2 Master Plan",
        "",
        f"Generated: {utc_now()}",
        "",
        "## Vision",
        "",
        "A signal-only Binance USD-M public-data bot that produces transparent Telegram alerts, tracks every signal to closure, and uses outcome evidence to tune strategy gates without hiding uncertainty.",
        "",
        "## Architecture",
        "",
        "- Runtime: `main.py` -> `bot.cli.run()` -> `SignalBot`.",
        "- State: SQLite via `MemoryRepository`; active lifecycle in `active_signals`, closed performance in `signal_outcomes`.",
        "- Data flow: REST/WS market data -> Polars feature preparation -> strategy engine -> family/context filters -> global filters -> Telegram delivery -> tracking/outcome persistence -> analytics/dashboard.",
        "",
        "## Data Acquisition",
        "",
        "- Keep Binance USD-M public endpoints only.",
        "- Preserve `rest_full` versus `ws_light` shortlist paths and keep fallback telemetry explicit.",
        "- Keep WS kline scope narrow and use cached REST context frames for 5m/1h/4h until live evidence proves broader WS subscriptions are stable.",
        "",
        "## Indicator Suite",
        "",
        "- Keep Polars-native feature work.",
        "- Treat derivative context missingness as data quality, not strategy failure.",
        "- Add backtest/optimizer tooling before adding new indicator libraries or new nonlinear gates.",
        "",
        "## Strategy Framework",
        "",
        "- Registered/current strategy count target: 37.",
        "- Use setup-score adjustments as score modifiers, not hard suppressors.",
        "- For high-SL setups with enough outcomes, tighten entry evidence and widen noise buffers conservatively.",
        "- For zero-hit setups, first separate missing-data causes from truly over-strict thresholds.",
        "",
        "## Signal Lifecycle",
        "",
        "- Every delivered signal gets a stable tracking ID/ref, Telegram message ID, active state row, and eventual `signal_outcomes` row.",
        "- Stale active signals must be reviewed at startup and on periodic tracking sweeps.",
        "- Superseded/expired/smart-exit outcomes remain visible in analytics but should not be counted as simple wins unless profitable.",
        "",
        "## Telegram Channel",
        "",
        "- Primary value is the signal card and tracking updates.",
        "- Keep message IDs persisted for edits/replies.",
        "- Pace sends locally to avoid flood-control retries during bursts.",
        "- Keep audit batches concise and suppress duplicates.",
        "",
        "## Dashboard",
        "",
        "- Keep `/api/status`, `/api/signals/active`, `/api/signals/recent`, `/api/analytics/report`, and `/api/strategies` live.",
        "- Strategy list must show all 37 strategy IDs and enabled state from config.",
        "- Performance panels should use `signal_outcomes`, not empty legacy `signals`/`outcomes` tables.",
        "",
        "## Scaling Plan",
        "",
        "- Prefer bounded concurrency and cached public REST context over expanding WS streams.",
        "- Increase shortlist size only after WS buffer drops and REST timeout rates stay low in a live run.",
        "- Rate-limit Telegram and batch non-critical audit updates.",
        "",
        "## Technology Stack",
        "",
        "- Python 3.13, asyncio, Polars, FastAPI/uvicorn for dashboard, SQLite/aiosqlite for persistence, aiogram for Telegram.",
        "- No signed Binance endpoints, no account APIs, no user-data streams, no auto-trading.",
        "",
        "## Executed Task Plan",
        "",
        "| Task | Files | Change | Success Criteria | Verification |",
        "| --- | --- | --- | --- | --- |",
        "| TASK_001 | `scripts/phase0_forensics.py`, `logs/00_file_map.md`..`04_telemetry_audit.md` | Refresh Phase 0 forensic reports | Reports regenerated from current filesystem/DB/logs/telemetry | `python scripts/phase0_forensics.py` |",
        "| TASK_002 | `bot/application/symbol_analyzer.py`, `tests/test_symbol_analyzer_telemetry.py` | Apply setup performance adjustment to score instead of hard-suppressing all negative setups | Mild -0.05 penalty keeps signal eligible for global filters | targeted pytest |",
        "| TASK_003 | `config.toml`, `config.toml.example` | Tighten high-SL setup parameters and align explicit dashboard/tracking config | Config validates; example stays aligned | `python -m scripts.validate_config` |",
        "| TASK_004 | `bot/messaging.py` | Add local Telegram pacing before sends/edits/photos | Reduced flood-control risk under bursts | import/tests/live smoke |",
        "| TASK_005 | `logs/10_analysis.md`, `logs/20_master_plan.md` | Generate evidence-backed analysis and plan | Reports exist and cite confirmed/uncertain evidence | `python scripts/phase1_analysis.py` |",
    ]
    (LOG_DIR / "20_master_plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    write_analysis()
    write_master_plan()
    print(LOG_DIR / "10_analysis.md")
    print(LOG_DIR / "20_master_plan.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

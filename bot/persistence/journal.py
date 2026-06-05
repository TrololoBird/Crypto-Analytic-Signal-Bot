"""Trader's journal — analytics over telemetry JSONL files.

Reads data/bot/telemetry/analysis/{selected,rejected,tracking_events}.jsonl
and returns a structured JournalReport. No writes, no network calls.

When a MemoryRepository is available, ``build_journal_report_from_repo`` is the
primary source; JSONL remains as a fallback with parity warnings on mismatch.

Also provides build_config_suggestions() which joins selected signals with their
outcomes (tp/sl/expired) and bins by key parameters to suggest config thresholds.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from bot.persistence.repository.memory import MemoryRepository

LOG = logging.getLogger("bot.journal")


@dataclass
class JournalReport:
    signals_sent: int = 0
    top_rejection_reasons: list[tuple[str, int]] = field(default_factory=list)
    setup_outcomes: dict[str, dict[str, int]] = field(default_factory=dict)
    # {setup_id: {event: count}} where event in tp1/tp2/sl/expired
    hourly_signal_counts: dict[int, int] = field(default_factory=dict)
    # {hour_of_day (0-23): count of signals sent}


JsonRow = dict[str, Any]
OutcomeItem = tuple[JsonRow, str]

# Canonical terminal tracking codes used in journal analytics (tp1/tp2/sl/expired).
_TRACKING_EVENT_ALIASES: dict[str, str] = {
    "tp1": "tp1",
    "tp1_hit": "tp1",
    "tp2": "tp2",
    "tp2_hit": "tp2",
    "sl": "sl",
    "stop_loss": "sl",
    "expired": "expired",
    "expired_pending": "expired",
    "expired_active": "expired",
}


def normalize_tracking_event(event: str | None) -> str | None:
    """Map telemetry event_type variants to canonical journal outcome codes."""
    key = str(event or "").strip().lower()
    if not key:
        return None
    return _TRACKING_EVENT_ALIASES.get(key)


def _iter_jsonl(path: Path) -> Iterator[JsonRow]:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield cast("JsonRow", row)


def _collect_analysis_dirs(telemetry_root: Path) -> list[Path]:
    dirs: list[Path] = []
    legacy = telemetry_root / "analysis"
    if legacy.exists():
        dirs.append(legacy)
    runs = telemetry_root / "runs"
    if runs.exists():
        for run_path in sorted(runs.iterdir()):
            ap = run_path / "analysis"
            if ap.exists():
                dirs.append(ap)
    return dirs


def _record_terminal_outcome(
    outcomes: dict[str, Counter[str]],
    seen_refs: set[str],
    *,
    tracking_ref: str,
    setup_id: str,
    canonical: str,
) -> None:
    """Count one terminal outcome per tracking_ref (R3 dedup)."""
    ref = str(tracking_ref or "").strip()
    if not ref or ref in seen_refs:
        return
    seen_refs.add(ref)
    outcomes[str(setup_id or "unknown")][canonical] += 1


def _build_journal_report_from_jsonl(telemetry_root: Path) -> JournalReport:
    """Build a JournalReport from JSONL files under telemetry_root."""
    analysis_dirs = _collect_analysis_dirs(telemetry_root)
    report = JournalReport()

    hourly: Counter[int] = Counter()
    signals_count = 0
    for analysis in analysis_dirs:
        for row in _iter_jsonl(analysis / "selected.jsonl"):
            signals_count += 1
            ts = row.get("ts", "")
            try:
                hour = int(ts[11:13])
                hourly[hour] += 1
            except (IndexError, ValueError):
                pass
    report.signals_sent = signals_count
    report.hourly_signal_counts = dict(sorted(hourly.items()))

    rejection_counter: Counter[str] = Counter()
    for analysis in analysis_dirs:
        for row in _iter_jsonl(analysis / "rejected.jsonl"):
            reason = row.get("reason") or row.get("filter") or "unknown"
            rejection_counter[reason] += 1
    report.top_rejection_reasons = rejection_counter.most_common(10)

    outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    seen_refs: set[str] = set()
    for analysis in analysis_dirs:
        for row in _iter_jsonl(analysis / "tracking_events.jsonl"):
            raw_event = row.get("event") or row.get("type") or row.get("event_type") or ""
            canonical = normalize_tracking_event(str(raw_event))
            if not canonical:
                continue
            _record_terminal_outcome(
                outcomes,
                seen_refs,
                tracking_ref=str(row.get("tracking_ref") or ""),
                setup_id=str(row.get("setup_id") or "unknown"),
                canonical=canonical,
            )
    report.setup_outcomes = {k: dict(v) for k, v in outcomes.items()}
    return report


def build_journal_report(telemetry_root: Path) -> JournalReport:
    """Build a JournalReport from JSONL telemetry (sync fallback path)."""
    return _build_journal_report_from_jsonl(telemetry_root)


def _journal_outcome_total(report: JournalReport) -> int:
    return sum(sum(events.values()) for events in report.setup_outcomes.values())


def _journal_parity_mismatch(
    repo_report: JournalReport,
    jsonl_report: JournalReport,
) -> dict[str, tuple[int, int]]:
    mismatches: dict[str, tuple[int, int]] = {}
    if repo_report.signals_sent != jsonl_report.signals_sent:
        mismatches["signals_sent"] = (repo_report.signals_sent, jsonl_report.signals_sent)
    repo_total = _journal_outcome_total(repo_report)
    jsonl_total = _journal_outcome_total(jsonl_report)
    if repo_total != jsonl_total:
        mismatches["terminal_outcomes"] = (repo_total, jsonl_total)
    return mismatches


def _warn_journal_parity_mismatch(
    repo_report: JournalReport,
    jsonl_report: JournalReport,
) -> None:
    mismatches = _journal_parity_mismatch(repo_report, jsonl_report)
    if mismatches:
        LOG.warning(
            "journal repo/jsonl parity mismatch | fields=%s",
            mismatches,
        )


async def build_journal_report_from_repo(repo: MemoryRepository) -> JournalReport:
    """Build a JournalReport from persisted repository data (primary path)."""
    report = JournalReport()
    stats = await repo.get_tracking_stats()
    report.signals_sent = int(stats.get("signals_sent") or 0)

    conn = repo._require_conn()
    async with conn.execute(
        """
        SELECT created_at
        FROM active_signals
        WHERE signal_message_id IS NOT NULL
        """
    ) as cursor:
        delivered_rows = await cursor.fetchall()

    hourly: Counter[int] = Counter()
    for row in delivered_rows:
        ts = str(row["created_at"] or "")
        with contextlib.suppress(IndexError, ValueError):
            hourly[int(ts[11:13])] += 1
    report.hourly_signal_counts = dict(sorted(hourly.items()))

    outcome_rows = await repo.get_signal_outcomes(last_days=None)
    outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    seen_refs: set[str] = set()
    for row in outcome_rows:
        canonical = normalize_tracking_event(str(row.get("result") or ""))
        if not canonical:
            continue
        _record_terminal_outcome(
            outcomes,
            seen_refs,
            tracking_ref=str(row.get("tracking_ref") or row.get("tracking_id") or ""),
            setup_id=str(row.get("setup_id") or "unknown"),
            canonical=canonical,
        )
    report.setup_outcomes = {k: dict(v) for k, v in outcomes.items()}
    return report


async def build_journal_report_primary(
    telemetry_root: Path,
    repo: MemoryRepository,
) -> JournalReport:
    """Build journal from repository; warn when JSONL telemetry differs."""
    repo_report = await build_journal_report_from_repo(repo)
    jsonl_report = _build_journal_report_from_jsonl(telemetry_root)
    _warn_journal_parity_mismatch(repo_report, jsonl_report)
    return repo_report


def build_config_suggestions(telemetry_root: Path) -> list[str]:
    """Analyse outcomes and suggest config parameter adjustments.

    Joins selected.jsonl signals with their tracking outcomes (tp1/tp2/sl/expired)
    using tracking_ref as the join key.  Bins by ATR%, score band, and RR band,
    then suggests thresholds where win rate diverges significantly across bins.

    Returns a list of suggestion lines ready to print.  Empty list = not enough data.
    """
    analysis_dirs = _collect_analysis_dirs(telemetry_root)

    # 1. Collect all selected signals keyed by tracking_ref
    signals: dict[str, dict[str, Any]] = {}
    for ad in analysis_dirs:
        for row in _iter_jsonl(ad / "selected.jsonl"):
            ref = row.get("tracking_ref") or row.get("signal", {}).get("tracking_ref")
            if not ref:
                continue
            sig = row.get("signal", row)
            signals[ref] = sig

    # 2. Collect terminal outcomes keyed by tracking_ref
    outcomes: dict[str, str] = {}  # ref -> "win" | "loss" | "expired"
    for ad in analysis_dirs:
        for row in _iter_jsonl(ad / "tracking_events.jsonl"):
            raw_event = row.get("event") or row.get("type") or row.get("event_type") or ""
            canonical = normalize_tracking_event(str(raw_event))
            ref = row.get("tracking_ref")
            if not ref or canonical is None:
                continue
            if ref not in outcomes:
                if canonical in {"tp1", "tp2"}:
                    outcomes[ref] = "win"
                elif canonical == "sl":
                    outcomes[ref] = "loss"
                else:
                    outcomes[ref] = "expired"

    # Only signals with a terminal outcome (tp/sl/expired) are usable
    resolved = [(signals[r], outcomes[r]) for r in outcomes if r in signals]
    total_resolved = len(resolved)
    MIN_BIN = 5
    suggestions: list[str] = []

    if total_resolved < 10:
        suggestions.append(
            f"[SUGGEST] Not enough resolved outcomes yet ({total_resolved}). "
            "Need at least 10 to generate suggestions."
        )
        return suggestions

    suggestions.append(f"[ADVISOR] Analysed {total_resolved} resolved signals\n")

    def _win_rate(items: list[OutcomeItem]) -> tuple[int, int, int]:
        """Return (wins, losses, expired)."""
        wins = sum(1 for _, o in items if o == "win")
        losses = sum(1 for _, o in items if o == "loss")
        expired = sum(1 for _, o in items if o == "expired")
        return wins, losses, expired

    def _wr_str(wins: int, losses: int) -> str:
        total = wins + losses
        if total == 0:
            return "n/a"
        return f"{wins / total * 100:.0f}%"

    # --- ATR % bins ---
    atr_bins: dict[str, list[OutcomeItem]] = defaultdict(list)
    for sig, outcome in resolved:
        atr = sig.get("atr_pct") or sig.get("signal", {}).get("atr_pct")
        if atr is None:
            continue
        atr = float(atr)
        if atr < 0.5:
            atr_bins["<0.50"].append((sig, outcome))
        elif atr < 0.75:
            atr_bins["0.50-0.75"].append((sig, outcome))
        elif atr < 1.0:
            atr_bins["0.75-1.00"].append((sig, outcome))
        elif atr < 1.5:
            atr_bins["1.00-1.50"].append((sig, outcome))
        else:
            atr_bins[">1.50"].append((sig, outcome))

    atr_lines = []
    for label in ["<0.50", "0.50-0.75", "0.75-1.00", "1.00-1.50", ">1.50"]:
        items = atr_bins.get(label, [])
        if len(items) >= MIN_BIN:
            wins, losses, expired = _win_rate(items)
            atr_lines.append(
                f"  ATR {label:>9}%  n={len(items):>3}  wr={_wr_str(wins, losses)}  "
                f"(wins={wins} sl={losses} exp={expired})"
            )
    if len(atr_lines) >= 2:
        suggestions.append("[SUGGEST] min_atr_pct — win rate by ATR band:")
        suggestions.extend(atr_lines)
        # Find the threshold where win rate drops below 40%
        threshold_hint = None
        for label in ["<0.50", "0.50-0.75"]:
            items = atr_bins.get(label, [])
            if len(items) >= MIN_BIN:
                wins, losses, _ = _win_rate(items)
                total = wins + losses
                if total > 0 and wins / total < 0.40:
                    threshold_hint = label
        if threshold_hint:
            suggestions.append(
                f"  → Low win rate in {threshold_hint} bin suggests raising min_atr_pct"
            )
        suggestions.append("")

    # --- Score bins ---
    score_bins: dict[str, list[OutcomeItem]] = defaultdict(list)
    for sig, outcome in resolved:
        score = sig.get("score") or sig.get("signal", {}).get("score")
        if score is None:
            continue
        score = float(score)
        if score < 0.65:
            score_bins["0.64-0.65"].append((sig, outcome))
        elif score < 0.68:
            score_bins["0.65-0.68"].append((sig, outcome))
        elif score < 0.72:
            score_bins["0.68-0.72"].append((sig, outcome))
        elif score < 0.78:
            score_bins["0.72-0.78"].append((sig, outcome))
        else:
            score_bins[">0.78"].append((sig, outcome))

    score_lines = []
    for label in ["0.64-0.65", "0.65-0.68", "0.68-0.72", "0.72-0.78", ">0.78"]:
        items = score_bins.get(label, [])
        if len(items) >= MIN_BIN:
            wins, losses, expired = _win_rate(items)
            score_lines.append(
                f"  score {label:>9}  n={len(items):>3}  wr={_wr_str(wins, losses)}  "
                f"(wins={wins} sl={losses} exp={expired})"
            )
    if len(score_lines) >= 2:
        suggestions.append("[SUGGEST] min_score — win rate by score band:")
        suggestions.extend(score_lines)
        # Suggest raising min_score if bottom band has poor win rate
        low_items = score_bins.get("0.64-0.65", []) + score_bins.get("0.65-0.68", [])
        high_items = score_bins.get("0.72-0.78", []) + score_bins.get(">0.78", [])
        if len(low_items) >= MIN_BIN and len(high_items) >= MIN_BIN:
            lw, ll, _ = _win_rate(low_items)
            hw, hl, _ = _win_rate(high_items)
            lt = lw + ll
            ht = hw + hl
            if lt > 0 and ht > 0 and (lw / lt) < (hw / ht) - 0.15:
                low_wr = _wr_str(lw, ll)
                high_wr = _wr_str(hw, hl)
                suggestions.append(
                    f"  → Clear quality gap (low={low_wr} vs high={high_wr}) "
                    "suggests raising min_score to 0.68+"
                )
        suggestions.append("")

    # --- RR bins ---
    rr_bins: dict[str, list[OutcomeItem]] = defaultdict(list)
    for sig, outcome in resolved:
        rr = sig.get("risk_reward") or sig.get("signal", {}).get("risk_reward")
        if rr is None:
            continue
        rr = float(rr)
        if rr < 2.5:
            rr_bins["1.9-2.5"].append((sig, outcome))
        elif rr < 3.5:
            rr_bins["2.5-3.5"].append((sig, outcome))
        elif rr < 5.0:
            rr_bins["3.5-5.0"].append((sig, outcome))
        else:
            rr_bins[">5.0"].append((sig, outcome))

    rr_lines = []
    for label in ["1.9-2.5", "2.5-3.5", "3.5-5.0", ">5.0"]:
        items = rr_bins.get(label, [])
        if len(items) >= MIN_BIN:
            wins, losses, expired = _win_rate(items)
            rr_lines.append(
                f"  RR {label:>9}  n={len(items):>3}  wr={_wr_str(wins, losses)}  "
                f"(wins={wins} sl={losses} exp={expired})"
            )
    if len(rr_lines) >= 2:
        suggestions.append("[SUGGEST] min_risk_reward — win rate by RR band:")
        suggestions.extend(rr_lines)
        suggestions.append("")

    # --- Setup performance with regime context ---
    setup_regime_bins: dict[str, list[OutcomeItem]] = defaultdict(list)
    for sig, outcome in resolved:
        setup_id = sig.get("setup_id") or sig.get("signal", {}).get("setup_id") or "unknown"
        regime = sig.get("bias_4h") or sig.get("signal", {}).get("bias_4h") or "neutral"
        key = f"{setup_id} / {regime}"
        setup_regime_bins[key].append((sig, outcome))

    setup_lines = []
    for key, items in sorted(setup_regime_bins.items(), key=lambda x: -len(x[1])):
        if len(items) >= MIN_BIN:
            wins, losses, _ = _win_rate(items)
            total = wins + losses
            wr_val = wins / total if total > 0 else None
            flag = " ← LOW" if wr_val is not None and wr_val < 0.35 else ""
            setup_lines.append(f"  {key:<35}  n={len(items):>3}  wr={_wr_str(wins, losses)}{flag}")
    if setup_lines:
        suggestions.append("[SUGGEST] Setup performance by regime (low wr = consider disabling):")
        suggestions.extend(setup_lines)
        suggestions.append("")

    if len(suggestions) <= 2:
        suggestions.append("[ADVISOR] Bins too small for suggestions — keep accumulating outcomes.")

    return suggestions


def print_journal_report(report: JournalReport) -> None:
    """Print a human-readable journal report to stdout."""
    print("=" * 60)
    print("TRADER'S JOURNAL")
    print("=" * 60)
    print(f"\nSignals sent: {report.signals_sent}")

    print("\nTop rejection reasons:")
    if not report.top_rejection_reasons:
        print("  (no data)")
    for reason, count in report.top_rejection_reasons:
        print(f"  {reason:<35} {count:>5}")

    print("\nSetup outcomes (tp1 / tp2 / sl / expired):")
    if not report.setup_outcomes:
        print("  (no tracking data yet)")
    for setup_id, events in sorted(report.setup_outcomes.items()):
        tp1 = events.get("tp1", 0)
        tp2 = events.get("tp2", 0)
        sl = events.get("sl", 0)
        expired = events.get("expired", 0)
        total = tp1 + tp2 + sl
        wr = f"{(tp1 + tp2) / total * 100:.0f}%" if total > 0 else "n/a"
        print(f"  {setup_id:<30} tp1={tp1} tp2={tp2} sl={sl} exp={expired} wr={wr}")

    print("\nSignals by hour of day (UTC):")
    if report.hourly_signal_counts:
        for hour in range(24):
            count = report.hourly_signal_counts.get(hour, 0)
            bar = "#" * count
            print(f"  {hour:02d}h  {count:>3}  {bar}")
    else:
        print("  (no data)")
    print()

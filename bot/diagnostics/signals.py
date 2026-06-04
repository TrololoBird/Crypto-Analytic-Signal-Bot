"""Runtime signal funnel diagnostics.

This module keeps lightweight, in-process counters for the live signal funnel.
It is intentionally independent from telemetry storage: telemetry records every
event for later analysis, while this class answers the immediate operational
question "why are signals not being generated right now?"
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_WINDOW_MINUTES = 15


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clean_key(value: object, *, default: str = "unknown") -> str:
    cleaned = str(value or "").strip()
    return cleaned or default


def _counter_to_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: int(value) for key, value in counter.most_common()}


def _sorted_set(values: set[str]) -> list[str]:
    return sorted(str(item) for item in values if str(item))


@dataclass(slots=True)
class _SignalDiagnosticWindow:
    """Mutable state for one diagnostic window."""

    started_at: datetime
    ends_at: datetime
    detector_runs_by_setup: Counter[str] = field(default_factory=Counter)
    detector_hits_by_setup: Counter[str] = field(default_factory=Counter)
    filter_rejects_by_reason: Counter[str] = field(default_factory=Counter)
    filter_rejects_by_setup: Counter[str] = field(default_factory=Counter)
    confirmation_rejects_by_reason: Counter[str] = field(default_factory=Counter)
    confirmation_rejects_by_setup: Counter[str] = field(default_factory=Counter)
    symbols_with_zero_detectors: set[str] = field(default_factory=set)
    symbols_with_stale_data: set[str] = field(default_factory=set)
    symbols_analyzed: set[str] = field(default_factory=set)
    candidates_by_setup: Counter[str] = field(default_factory=Counter)
    delivered_by_setup: Counter[str] = field(default_factory=Counter)
    stage_rejects: Counter[str] = field(default_factory=Counter)
    routing_skips_by_reason: Counter[str] = field(default_factory=Counter)
    routing_skips_by_setup: Counter[str] = field(default_factory=Counter)
    atr_samples_by_setup: dict[str, dict[str, list[float]]] = field(default_factory=dict)

    def total_detector_runs(self) -> int:
        return int(sum(self.detector_runs_by_setup.values()))

    def total_detector_hits(self) -> int:
        return int(sum(self.detector_hits_by_setup.values()))

    def total_filter_rejects(self) -> int:
        return int(sum(self.filter_rejects_by_reason.values()))

    def total_confirmation_rejects(self) -> int:
        return int(sum(self.confirmation_rejects_by_reason.values()))

    def total_candidates(self) -> int:
        return int(sum(self.candidates_by_setup.values()))

    def total_delivered(self) -> int:
        return int(sum(self.delivered_by_setup.values()))


class SignalDiagnostics:
    """Thread-safe rolling diagnostics for the signal generation funnel.

    Parameters
    ----------
    window_minutes:
        Length of each rolling diagnostic window. The default is fifteen
        minutes, matching the primary strategy timeframe.

    Notes
    -----
    The object is safe to call from async tasks and executor-backed callbacks.
    It uses a regular ``threading.Lock`` and performs only tiny in-memory
    updates while the lock is held.
    """

    def __init__(self, *, window_minutes: int = DEFAULT_WINDOW_MINUTES) -> None:
        self.window_minutes = max(1, int(window_minutes))
        self._lock = threading.Lock()
        self._window = self._new_window(_utc_now())
        self._previous_windows: list[dict[str, Any]] = []

    def record_detector_run(self, setup_id: str) -> None:
        """Record that a detector was evaluated.

        Parameters
        ----------
        setup_id:
            Strategy/setup identifier emitted by the strategy registry.
        """
        setup = _clean_key(setup_id)
        with self._lock:
            window = self._current_window_unlocked()
            window.detector_runs_by_setup[setup] += 1

    def record_detector_hit(self, setup_id: str) -> None:
        """Record that a detector produced a signal candidate."""
        setup = _clean_key(setup_id)
        with self._lock:
            window = self._current_window_unlocked()
            window.detector_hits_by_setup[setup] += 1

    def record_filter_reject(self, setup_id: str, reason: str) -> None:
        """Record a global-filter rejection."""
        setup = _clean_key(setup_id)
        reject_reason = _clean_key(reason)
        with self._lock:
            window = self._current_window_unlocked()
            window.filter_rejects_by_setup[setup] += 1
            window.filter_rejects_by_reason[reject_reason] += 1
            window.stage_rejects["filters"] += 1
            if reject_reason.startswith("stale_"):
                # The caller may also record the symbol. This stage-level flag
                # keeps stale-data failures visible even when symbol context is
                # unavailable at the recording site.
                window.symbols_with_stale_data.add("unknown")

    def record_confirmation_reject(self, setup_id: str, reason: str) -> None:
        """Record a family or lower-timeframe confirmation rejection."""
        setup = _clean_key(setup_id)
        reject_reason = _clean_key(reason)
        with self._lock:
            window = self._current_window_unlocked()
            window.confirmation_rejects_by_setup[setup] += 1
            window.confirmation_rejects_by_reason[reject_reason] += 1
            window.stage_rejects["confirmation"] += 1

    def record_routing_skip(self, setup_id: str, reason: str) -> None:
        """Record an engine routing skip (lane exclusion, fit filter, schedule)."""
        setup = _clean_key(setup_id)
        reject_reason = _clean_key(reason)
        with self._lock:
            window = self._current_window_unlocked()
            window.routing_skips_by_setup[setup] += 1
            window.routing_skips_by_reason[reject_reason] += 1
            window.stage_rejects["routing_skip"] += 1

    def record_stale_symbol(self, symbol: str) -> None:
        """Record a symbol with stale required market data."""
        normalized = _clean_key(symbol).upper()
        with self._lock:
            window = self._current_window_unlocked()
            window.symbols_with_stale_data.add(normalized)

    def record_symbol_analyzed(self, symbol: str) -> None:
        """Record that a symbol entered modern analysis."""
        normalized = _clean_key(symbol).upper()
        with self._lock:
            window = self._current_window_unlocked()
            window.symbols_analyzed.add(normalized)

    def record_zero_detector_symbol(self, symbol: str) -> None:
        """Record a symbol for which no detector results were returned."""
        normalized = _clean_key(symbol).upper()
        with self._lock:
            window = self._current_window_unlocked()
            window.symbols_with_zero_detectors.add(normalized)

    def record_candidate(self, setup_id: str) -> None:
        """Record that a signal passed filters and became a candidate."""
        setup = _clean_key(setup_id)
        with self._lock:
            window = self._current_window_unlocked()
            window.candidates_by_setup[setup] += 1

    def record_delivered(self, setup_id: str) -> None:
        """Record a delivered signal.

        Delivery integration is optional. The method exists so delivery code can
        use the same diagnostics object without importing telemetry internals.
        """
        setup = _clean_key(setup_id)
        with self._lock:
            window = self._current_window_unlocked()
            window.delivered_by_setup[setup] += 1

    def record_delivery_stage_reject(
        self,
        stage: str,
        reason: str,
        *,
        setup_id: str | None = None,
    ) -> None:
        """Record a post-candidate delivery funnel rejection (contract/tier/delivery)."""
        stage_key = _clean_key(stage)
        reject_reason = _clean_key(reason)
        setup = _clean_key(setup_id) if setup_id else "unknown"
        with self._lock:
            window = self._current_window_unlocked()
            window.stage_rejects[stage_key] += 1
            window.filter_rejects_by_reason[f"{stage_key}:{reject_reason}"] += 1
            window.filter_rejects_by_setup[setup] += 1

    def record_atr_sample(self, setup_id: str, atr_pct: float, *, passed: bool) -> None:
        """Record an ATR sample for threshold calibration.

        Parameters
        ----------
        setup_id:
            Strategy/setup identifier for the candidate being filtered.
        atr_pct:
            Current ATR percentage observed at the filter gate.
        passed:
            ``True`` when the sample passed the effective ATR floor, otherwise
            ``False``.

        Notes
        -----
        Only the most recent 200 passing and 200 failing samples are retained
        per setup. This keeps memory bounded while still exposing enough
        distribution shape to detect a stale ``filters.min_atr_pct`` setting.
        """
        setup = _clean_key(setup_id)
        state = "pass" if passed else "fail"
        try:
            sample = round(float(atr_pct), 4)
        except (TypeError, ValueError):
            return
        with self._lock:
            window = self._current_window_unlocked()
            buckets = window.atr_samples_by_setup.setdefault(
                setup,
                {"pass": [], "fail": []},
            )
            values = buckets.setdefault(state, [])
            values.append(sample)
            if len(values) > 200:
                buckets[state] = values[-200:]

    def get_summary(self) -> dict[str, Any]:
        """Return all counters for the current window.

        Returns
        -------
        dict
            JSON-serializable summary with counters, sets, totals, and derived
            funnel efficiency metrics.
        """
        with self._lock:
            window = self._current_window_unlocked()
            return self._summary_for_window_unlocked(window)

    def get_atr_summary(self) -> dict[str, dict[str, float | int]]:
        """Return ATR pass/fail medians per setup.

        Returns
        -------
        dict
            Mapping of setup id to summary fields such as ``pass_median``,
            ``pass_count``, ``fail_median``, and ``fail_count``.
        """
        with self._lock:
            window = self._current_window_unlocked()
            return self._atr_summary_for_window_unlocked(window)

    def get_pipeline_efficiency(self) -> dict[str, Any]:
        """Compute pipeline efficiency metrics for the current window.

        Returns
        -------
        dict
            Summary with detector totals, hit rate, filter pass rate, top
            rejection reasons, setups with detector runs but zero hits, and
            ATR calibration data.
        """
        with self._lock:
            window = self._current_window_unlocked()
            detector_runs = window.total_detector_runs()
            detector_hits = window.total_detector_hits()
            candidates = window.total_candidates()
            zero_hit_setups = [
                setup_id
                for setup_id, runs in window.detector_runs_by_setup.items()
                if runs > 0 and window.detector_hits_by_setup.get(setup_id, 0) == 0
            ]
            return {
                "detector_run_total": detector_runs,
                "detector_hit_total": detector_hits,
                "hit_rate": round(detector_hits / detector_runs, 6) if detector_runs else 0.0,
                "filter_pass_rate": round(candidates / detector_hits, 6) if detector_hits else 0.0,
                "top_rejects": [
                    (reason, int(count))
                    for reason, count in window.filter_rejects_by_reason.most_common(5)
                ],
                "top_zero_detector_setups": sorted(zero_hit_setups)[:20],
                "atr_calibration": self._atr_summary_for_window_unlocked(window),
            }

    def log_summary(self, logger: Any) -> None:
        """Log the current diagnostic summary.

        The method emits ``INFO`` when detector hits are present and ``WARNING``
        when the current window has detector runs but zero hits.
        """
        summary = self.get_summary()
        detector_runs = int(summary.get("detector_runs_total") or 0)
        detector_hits = int(summary.get("detector_hits_total") or 0)
        candidates = int(summary.get("candidates_total") or 0)
        stale_symbols = summary.get("symbols_with_stale_data", [])
        top_filter_reasons = summary.get("top_filter_reject_reasons", [])
        message = (
            "signal diagnostics | window_start=%s detector_runs=%d hits=%d "
            "candidates=%d hit_rate=%.4f stale_symbols=%s top_filter_rejects=%s"
        )
        args = (
            summary.get("window_started_at"),
            detector_runs,
            detector_hits,
            candidates,
            float(summary.get("detector_hit_rate") or 0.0),
            stale_symbols[:8] if isinstance(stale_symbols, list) else stale_symbols,
            top_filter_reasons[:5] if isinstance(top_filter_reasons, list) else top_filter_reasons,
        )
        if detector_runs > 0 and detector_hits == 0:
            logger.warning(message, *args)
        elif detector_hits > 0 or candidates > 0:
            logger.info(message, *args)

    def generate_markdown_report(self) -> str:
        """Return a Markdown report for the current diagnostic window.

        Returns
        -------
        str
            Markdown text with hit-rate, rejection, stale-symbol, and pipeline
            efficiency sections.
        """
        summary = self.get_summary()
        lines: list[str] = []
        lines.append("# Signal Diagnostics")
        lines.append("")
        lines.append(f"- Window: `{summary['window_started_at']}` to `{summary['window_ends_at']}`")
        lines.append(f"- Detector runs: `{summary['detector_runs_total']}`")
        lines.append(f"- Detector hits: `{summary['detector_hits_total']}`")
        lines.append(f"- Candidates: `{summary['candidates_total']}`")
        lines.append(f"- Delivered: `{summary['delivered_total']}`")
        lines.append(f"- Hit rate: `{summary['detector_hit_rate']}`")
        lines.append(f"- Pipeline efficiency: `{summary['pipeline_efficiency']}`")
        lines.append("")
        lines.append("## Top Setups By Hit Rate")
        lines.extend(self._markdown_table(summary.get("setup_hit_rates", [])[:10]))
        lines.append("")
        lines.append("## Top Filter Reject Reasons")
        reason_rows = [
            {"reason": item["key"], "count": item["count"]}
            for item in summary.get("top_filter_reject_reasons", [])[:10]
        ]
        lines.extend(self._markdown_table(reason_rows))
        lines.append("")
        lines.append("## Stale Symbols")
        stale_rows = [{"symbol": symbol} for symbol in summary.get("symbols_with_stale_data", [])]
        lines.extend(self._markdown_table(stale_rows[:25]))
        lines.append("")
        lines.append("## ATR Calibration")
        atr_rows: list[dict[str, Any]] = []
        atr_summary = summary.get("atr_summary", {})
        if isinstance(atr_summary, dict):
            for setup_id, values in sorted(atr_summary.items()):
                row = {"setup_id": setup_id}
                if isinstance(values, dict):
                    row.update(values)
                atr_rows.append(row)
        lines.extend(self._markdown_table(atr_rows[:25]))
        lines.append("")
        lines.append("## Zero Detector Symbols")
        zero_rows = [
            {"symbol": symbol} for symbol in summary.get("symbols_with_zero_detectors", [])
        ]
        lines.extend(self._markdown_table(zero_rows[:25]))
        return "\n".join(lines) + "\n"

    def compare_windows(self, other: SignalDiagnostics) -> dict[str, Any]:
        """Compare this diagnostic window against another instance.

        Parameters
        ----------
        other:
            Another ``SignalDiagnostics`` instance.

        Returns
        -------
        dict
            Delta summary for totals and counters. Positive values mean the
            current instance has more events than ``other``.
        """
        current = self.get_summary()
        baseline = other.get_summary()
        counter_keys = (
            "detector_runs_by_setup",
            "detector_hits_by_setup",
            "filter_rejects_by_reason",
            "filter_rejects_by_setup",
            "confirmation_rejects_by_reason",
        )
        deltas: dict[str, Any] = {
            "detector_runs_total_delta": int(current["detector_runs_total"])
            - int(baseline["detector_runs_total"]),
            "detector_hits_total_delta": int(current["detector_hits_total"])
            - int(baseline["detector_hits_total"]),
            "candidates_total_delta": int(current["candidates_total"])
            - int(baseline["candidates_total"]),
            "delivered_total_delta": int(current["delivered_total"])
            - int(baseline["delivered_total"]),
        }
        for key in counter_keys:
            current_counter = Counter(current.get(key, {}))
            baseline_counter = Counter(baseline.get(key, {}))
            delta_counter = current_counter - baseline_counter
            negative_counter = baseline_counter - current_counter
            merged = {item_key: int(value) for item_key, value in delta_counter.items()}
            merged.update({item_key: -int(value) for item_key, value in negative_counter.items()})
            deltas[f"{key}_delta"] = dict(sorted(merged.items()))
        return deltas

    def export_jsonl(self, path: Path) -> None:
        """Append the current summary as one JSON line.

        Parameters
        ----------
        path:
            File to append to. The parent directory is created when missing.
        """
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.get_summary(), ensure_ascii=True, default=str) + "\n")

    def reset_window(self) -> dict[str, Any]:
        """Reset all counters and return the cleared window snapshot.

        Returns
        -------
        dict
            Summary for the diagnostic window that was active before reset.
        """
        with self._lock:
            window = self._current_window_unlocked()
            snapshot = self._summary_for_window_unlocked(window)
            self._previous_windows.append(snapshot)
            self._previous_windows = self._previous_windows[-8:]
            self._window = self._new_window(_utc_now())
            return snapshot

    def reset(self) -> None:
        """Start a fresh diagnostic window immediately."""
        with self._lock:
            self._previous_windows.append(self._summary_for_window_unlocked(self._window))
            self._previous_windows = self._previous_windows[-8:]
            self._window = self._new_window(_utc_now())

    def previous_windows(self) -> list[dict[str, Any]]:
        """Return summaries for recently rolled windows."""
        with self._lock:
            self._current_window_unlocked()
            return list(self._previous_windows)

    def _current_window_unlocked(self) -> _SignalDiagnosticWindow:
        now = _utc_now()
        if now >= self._window.ends_at:
            self._previous_windows.append(self._summary_for_window_unlocked(self._window))
            self._previous_windows = self._previous_windows[-8:]
            self._window = self._new_window(now)
        return self._window

    def _new_window(self, now: datetime) -> _SignalDiagnosticWindow:
        window = timedelta(minutes=self.window_minutes)
        return _SignalDiagnosticWindow(started_at=now, ends_at=now + window)

    def _summary_for_window_unlocked(self, window: _SignalDiagnosticWindow) -> dict[str, Any]:
        detector_runs_total = window.total_detector_runs()
        detector_hits_total = window.total_detector_hits()
        candidates_total = window.total_candidates()
        delivered_total = window.total_delivered()
        return {
            "window_started_at": window.started_at.isoformat(),
            "window_ends_at": window.ends_at.isoformat(),
            "window_minutes": self.window_minutes,
            "detector_runs_by_setup": _counter_to_dict(window.detector_runs_by_setup),
            "detector_hits_by_setup": _counter_to_dict(window.detector_hits_by_setup),
            "filter_rejects_by_reason": _counter_to_dict(window.filter_rejects_by_reason),
            "filter_rejects_by_setup": _counter_to_dict(window.filter_rejects_by_setup),
            "confirmation_rejects_by_reason": _counter_to_dict(
                window.confirmation_rejects_by_reason
            ),
            "confirmation_rejects_by_setup": _counter_to_dict(window.confirmation_rejects_by_setup),
            "stage_rejects": _counter_to_dict(window.stage_rejects),
            "routing_skips_by_reason": _counter_to_dict(window.routing_skips_by_reason),
            "routing_skips_by_setup": _counter_to_dict(window.routing_skips_by_setup),
            "routing_skips_total": int(sum(window.routing_skips_by_reason.values())),
            "candidates_by_setup": _counter_to_dict(window.candidates_by_setup),
            "delivered_by_setup": _counter_to_dict(window.delivered_by_setup),
            "symbols_with_zero_detectors": _sorted_set(window.symbols_with_zero_detectors),
            "symbols_with_stale_data": _sorted_set(window.symbols_with_stale_data),
            "symbols_analyzed": _sorted_set(window.symbols_analyzed),
            "detector_runs_total": detector_runs_total,
            "detector_hits_total": detector_hits_total,
            "filter_rejects_total": window.total_filter_rejects(),
            "confirmation_rejects_total": window.total_confirmation_rejects(),
            "candidates_total": candidates_total,
            "delivered_total": delivered_total,
            "detector_hit_rate": round(
                detector_hits_total / detector_runs_total,
                6,
            )
            if detector_runs_total
            else 0.0,
            "pipeline_efficiency": round(
                candidates_total / detector_runs_total,
                6,
            )
            if detector_runs_total
            else 0.0,
            "delivery_efficiency": round(
                delivered_total / candidates_total,
                6,
            )
            if candidates_total
            else 0.0,
            "top_filter_reject_reasons": self._top_counter_rows(window.filter_rejects_by_reason),
            "top_confirmation_reject_reasons": self._top_counter_rows(
                window.confirmation_rejects_by_reason
            ),
            "setup_hit_rates": self._setup_hit_rate_rows(window),
            "atr_summary": self._atr_summary_for_window_unlocked(window),
        }

    @staticmethod
    def _median(values: list[float]) -> float:
        ordered = sorted(values)
        if not ordered:
            return 0.0
        index = len(ordered) // 2
        return float(ordered[index])

    def _atr_summary_for_window_unlocked(
        self,
        window: _SignalDiagnosticWindow,
    ) -> dict[str, dict[str, float | int]]:
        result: dict[str, dict[str, float | int]] = {}
        for setup_id, buckets in window.atr_samples_by_setup.items():
            setup_summary: dict[str, float | int] = {}
            for state, values in buckets.items():
                if not values:
                    continue
                setup_summary[f"{state}_median"] = round(self._median(values), 4)
                setup_summary[f"{state}_count"] = len(values)
            if setup_summary:
                result[setup_id] = setup_summary
        return result

    @staticmethod
    def _top_counter_rows(counter: Counter[str], *, limit: int = 20) -> list[dict[str, Any]]:
        return [{"key": key, "count": int(value)} for key, value in counter.most_common(limit)]

    @staticmethod
    def _setup_hit_rate_rows(window: _SignalDiagnosticWindow) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        setup_ids = set(window.detector_runs_by_setup) | set(window.detector_hits_by_setup)
        for setup_id in sorted(setup_ids):
            runs = int(window.detector_runs_by_setup.get(setup_id, 0))
            hits = int(window.detector_hits_by_setup.get(setup_id, 0))
            rows.append(
                {
                    "setup_id": setup_id,
                    "runs": runs,
                    "hits": hits,
                    "hit_rate_pct": round((hits / runs) * 100.0, 2) if runs else 0.0,
                }
            )
        rows.sort(key=lambda row: (row["hit_rate_pct"], row["hits"], row["runs"]), reverse=True)
        return rows

    @staticmethod
    def _markdown_table(rows: list[dict[str, Any]]) -> list[str]:
        if not rows:
            return ["_No rows._"]
        columns = list(rows[0].keys())
        output = [
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
        ]
        output.extend(
            "| " + " | ".join(str(row.get(column, "")) for column in columns) + " |" for row in rows
        )
        return output


_GLOBAL_DIAGNOSTICS: list[SignalDiagnostics | None] = [None]


def get_global_diagnostics() -> SignalDiagnostics | None:
    """Return the process-wide diagnostics object, if initialized."""
    return _GLOBAL_DIAGNOSTICS[0]


def set_global_diagnostics(diag: SignalDiagnostics) -> None:
    """Register the process-wide diagnostics object."""
    _GLOBAL_DIAGNOSTICS[0] = diag

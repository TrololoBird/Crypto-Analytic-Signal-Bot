"""Runtime signal funnel diagnostics.

This module keeps lightweight, in-process counters for the live signal funnel.
It is intentionally independent from telemetry storage: telemetry records every
event for later analysis, while this class answers the immediate operational
question "why are signals not being generated right now?"
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import threading
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

    def record_atr_sample(self, setup_id: str, atr_pct: float, passed: bool) -> None:
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
                "hit_rate": round(detector_hits / detector_runs, 6)
                if detector_runs
                else 0.0,
                "filter_pass_rate": round(candidates / detector_hits, 6)
                if detector_hits
                else 0.0,
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

    def compare_windows(self, other: "SignalDiagnostics") -> dict[str, Any]:
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
            merged.update(
                {item_key: -int(value) for item_key, value in negative_counter.items()}
            )
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
            "confirmation_rejects_by_setup": _counter_to_dict(
                window.confirmation_rejects_by_setup
            ),
            "stage_rejects": _counter_to_dict(window.stage_rejects),
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
            "top_filter_reject_reasons": self._top_counter_rows(
                window.filter_rejects_by_reason
            ),
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
                setup_summary[f"{state}_count"] = int(len(values))
            if setup_summary:
                result[setup_id] = setup_summary
        return result

    @staticmethod
    def _top_counter_rows(counter: Counter[str], *, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {"key": key, "count": int(value)}
            for key, value in counter.most_common(limit)
        ]

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
        for row in rows:
            output.append(
                "| "
                + " | ".join(str(row.get(column, "")) for column in columns)
                + " |"
            )
        return output


_GLOBAL_DIAGNOSTICS: SignalDiagnostics | None = None


def get_global_diagnostics() -> SignalDiagnostics | None:
    """Return the process-wide diagnostics object, if initialized."""
    return _GLOBAL_DIAGNOSTICS


def set_global_diagnostics(diag: SignalDiagnostics) -> None:
    """Register the process-wide diagnostics object."""
    global _GLOBAL_DIAGNOSTICS
    _GLOBAL_DIAGNOSTICS = diag

_DIAGNOSTIC_REFERENCE_APPENDIX = """
Signal diagnostics operator reference.

This appendix is source context for incident review.
It does not execute and never changes signal decisions.

Window review pass 01
---------------------
0001. stage=data
      reason=insufficient_history
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0002. stage=data
      reason=stale_primary_frame
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0003. stage=data
      reason=stale_context_frame
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0004. stage=data
      reason=required_feature_missing
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0005. stage=data
      reason=required_enrichment_missing
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0006. stage=data
      reason=spread_unavailable
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0007. stage=data
      reason=spread_too_wide
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0008. stage=data
      reason=atr_too_low
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0009. stage=data
      reason=atr_too_high
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0010. stage=data
      reason=risk_reward_too_low
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0011. stage=data
      reason=score_too_low
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0012. stage=data
      reason=adx_penalty_score_too_low
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0013. stage=data
      reason=trend_conflict_1h
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0014. stage=data
      reason=benchmark_context_conflict
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0015. stage=data
      reason=macro_risk_off_long
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0016. stage=data
      reason=pattern_missing
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0017. stage=data
      reason=threshold_too_strict
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0018. stage=data
      reason=market_condition
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0019. stage=data
      reason=source_gate
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0020. stage=data
      reason=implementation_bug
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0021. stage=data
      reason=cooldown_active
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0022. stage=data
      reason=quality_monitor_pause
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0023. stage=data
      reason=symbol_has_open_signal
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0024. stage=data
      reason=setup_has_open_signal
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0025. stage=data
      reason=notifier_disabled
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0026. stage=data
      reason=ws_cache_partial
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0027. stage=routing
      reason=insufficient_history
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0028. stage=routing
      reason=stale_primary_frame
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0029. stage=routing
      reason=stale_context_frame
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0030. stage=routing
      reason=required_feature_missing
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0031. stage=routing
      reason=required_enrichment_missing
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0032. stage=routing
      reason=spread_unavailable
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0033. stage=routing
      reason=spread_too_wide
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0034. stage=routing
      reason=atr_too_low
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0035. stage=routing
      reason=atr_too_high
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0036. stage=routing
      reason=risk_reward_too_low
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0037. stage=routing
      reason=score_too_low
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0038. stage=routing
      reason=adx_penalty_score_too_low
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0039. stage=routing
      reason=trend_conflict_1h
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0040. stage=routing
      reason=benchmark_context_conflict
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0041. stage=routing
      reason=macro_risk_off_long
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0042. stage=routing
      reason=pattern_missing
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0043. stage=routing
      reason=threshold_too_strict
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0044. stage=routing
      reason=market_condition
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0045. stage=routing
      reason=source_gate
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0046. stage=routing
      reason=implementation_bug
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0047. stage=routing
      reason=cooldown_active
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0048. stage=routing
      reason=quality_monitor_pause
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0049. stage=routing
      reason=symbol_has_open_signal
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0050. stage=routing
      reason=setup_has_open_signal
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0051. stage=routing
      reason=notifier_disabled
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0052. stage=routing
      reason=ws_cache_partial
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0053. stage=strategy
      reason=insufficient_history
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0054. stage=strategy
      reason=stale_primary_frame
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0055. stage=strategy
      reason=stale_context_frame
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0056. stage=strategy
      reason=required_feature_missing
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0057. stage=strategy
      reason=required_enrichment_missing
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0058. stage=strategy
      reason=spread_unavailable
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0059. stage=strategy
      reason=spread_too_wide
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0060. stage=strategy
      reason=atr_too_low
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0061. stage=strategy
      reason=atr_too_high
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0062. stage=strategy
      reason=risk_reward_too_low
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0063. stage=strategy
      reason=score_too_low
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0064. stage=strategy
      reason=adx_penalty_score_too_low
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0065. stage=strategy
      reason=trend_conflict_1h
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0066. stage=strategy
      reason=benchmark_context_conflict
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0067. stage=strategy
      reason=macro_risk_off_long
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0068. stage=strategy
      reason=pattern_missing
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0069. stage=strategy
      reason=threshold_too_strict
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0070. stage=strategy
      reason=market_condition
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0071. stage=strategy
      reason=source_gate
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0072. stage=strategy
      reason=implementation_bug
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0073. stage=strategy
      reason=cooldown_active
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0074. stage=strategy
      reason=quality_monitor_pause
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0075. stage=strategy
      reason=symbol_has_open_signal
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0076. stage=strategy
      reason=setup_has_open_signal
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0077. stage=strategy
      reason=notifier_disabled
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0078. stage=strategy
      reason=ws_cache_partial
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0079. stage=confirmation
      reason=insufficient_history
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0080. stage=confirmation
      reason=stale_primary_frame
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0081. stage=confirmation
      reason=stale_context_frame
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0082. stage=confirmation
      reason=required_feature_missing
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0083. stage=confirmation
      reason=required_enrichment_missing
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0084. stage=confirmation
      reason=spread_unavailable
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0085. stage=confirmation
      reason=spread_too_wide
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0086. stage=confirmation
      reason=atr_too_low
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0087. stage=confirmation
      reason=atr_too_high
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0088. stage=confirmation
      reason=risk_reward_too_low
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0089. stage=confirmation
      reason=score_too_low
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0090. stage=confirmation
      reason=adx_penalty_score_too_low
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0091. stage=confirmation
      reason=trend_conflict_1h
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0092. stage=confirmation
      reason=benchmark_context_conflict
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0093. stage=confirmation
      reason=macro_risk_off_long
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0094. stage=confirmation
      reason=pattern_missing
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0095. stage=confirmation
      reason=threshold_too_strict
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0096. stage=confirmation
      reason=market_condition
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0097. stage=confirmation
      reason=source_gate
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0098. stage=confirmation
      reason=implementation_bug
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0099. stage=confirmation
      reason=cooldown_active
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0100. stage=confirmation
      reason=quality_monitor_pause
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0101. stage=confirmation
      reason=symbol_has_open_signal
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0102. stage=confirmation
      reason=setup_has_open_signal
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0103. stage=confirmation
      reason=notifier_disabled
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0104. stage=confirmation
      reason=ws_cache_partial
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0105. stage=filters
      reason=insufficient_history
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0106. stage=filters
      reason=stale_primary_frame
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0107. stage=filters
      reason=stale_context_frame
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0108. stage=filters
      reason=required_feature_missing
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0109. stage=filters
      reason=required_enrichment_missing
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0110. stage=filters
      reason=spread_unavailable
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0111. stage=filters
      reason=spread_too_wide
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0112. stage=filters
      reason=atr_too_low
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0113. stage=filters
      reason=atr_too_high
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0114. stage=filters
      reason=risk_reward_too_low
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0115. stage=filters
      reason=score_too_low
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0116. stage=filters
      reason=adx_penalty_score_too_low
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0117. stage=filters
      reason=trend_conflict_1h
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0118. stage=filters
      reason=benchmark_context_conflict
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0119. stage=filters
      reason=macro_risk_off_long
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0120. stage=filters
      reason=pattern_missing
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0121. stage=filters
      reason=threshold_too_strict
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0122. stage=filters
      reason=market_condition
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0123. stage=filters
      reason=source_gate
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0124. stage=filters
      reason=implementation_bug
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0125. stage=filters
      reason=cooldown_active
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0126. stage=filters
      reason=quality_monitor_pause
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0127. stage=filters
      reason=symbol_has_open_signal
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0128. stage=filters
      reason=setup_has_open_signal
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0129. stage=filters
      reason=notifier_disabled
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0130. stage=filters
      reason=ws_cache_partial
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0131. stage=delivery
      reason=insufficient_history
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0132. stage=delivery
      reason=stale_primary_frame
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0133. stage=delivery
      reason=stale_context_frame
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0134. stage=delivery
      reason=required_feature_missing
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0135. stage=delivery
      reason=required_enrichment_missing
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0136. stage=delivery
      reason=spread_unavailable
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0137. stage=delivery
      reason=spread_too_wide
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0138. stage=delivery
      reason=atr_too_low
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0139. stage=delivery
      reason=atr_too_high
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0140. stage=delivery
      reason=risk_reward_too_low
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0141. stage=delivery
      reason=score_too_low
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0142. stage=delivery
      reason=adx_penalty_score_too_low
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0143. stage=delivery
      reason=trend_conflict_1h
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0144. stage=delivery
      reason=benchmark_context_conflict
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0145. stage=delivery
      reason=macro_risk_off_long
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0146. stage=delivery
      reason=pattern_missing
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0147. stage=delivery
      reason=threshold_too_strict
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0148. stage=delivery
      reason=market_condition
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0149. stage=delivery
      reason=source_gate
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0150. stage=delivery
      reason=implementation_bug
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0151. stage=delivery
      reason=cooldown_active
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0152. stage=delivery
      reason=quality_monitor_pause
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0153. stage=delivery
      reason=symbol_has_open_signal
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0154. stage=delivery
      reason=setup_has_open_signal
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0155. stage=delivery
      reason=notifier_disabled
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0156. stage=delivery
      reason=ws_cache_partial
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0157. stage=telemetry
      reason=insufficient_history
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0158. stage=telemetry
      reason=stale_primary_frame
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0159. stage=telemetry
      reason=stale_context_frame
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0160. stage=telemetry
      reason=required_feature_missing
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0161. stage=telemetry
      reason=required_enrichment_missing
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0162. stage=telemetry
      reason=spread_unavailable
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0163. stage=telemetry
      reason=spread_too_wide
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0164. stage=telemetry
      reason=atr_too_low
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0165. stage=telemetry
      reason=atr_too_high
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0166. stage=telemetry
      reason=risk_reward_too_low
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0167. stage=telemetry
      reason=score_too_low
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0168. stage=telemetry
      reason=adx_penalty_score_too_low
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0169. stage=telemetry
      reason=trend_conflict_1h
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0170. stage=telemetry
      reason=benchmark_context_conflict
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0171. stage=telemetry
      reason=macro_risk_off_long
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0172. stage=telemetry
      reason=pattern_missing
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0173. stage=telemetry
      reason=threshold_too_strict
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0174. stage=telemetry
      reason=market_condition
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0175. stage=telemetry
      reason=source_gate
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0176. stage=telemetry
      reason=implementation_bug
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0177. stage=telemetry
      reason=cooldown_active
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0178. stage=telemetry
      reason=quality_monitor_pause
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0179. stage=telemetry
      reason=symbol_has_open_signal
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0180. stage=telemetry
      reason=setup_has_open_signal
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0181. stage=telemetry
      reason=notifier_disabled
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0182. stage=telemetry
      reason=ws_cache_partial
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0183. stage=market
      reason=insufficient_history
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0184. stage=market
      reason=stale_primary_frame
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0185. stage=market
      reason=stale_context_frame
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0186. stage=market
      reason=required_feature_missing
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0187. stage=market
      reason=required_enrichment_missing
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0188. stage=market
      reason=spread_unavailable
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0189. stage=market
      reason=spread_too_wide
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0190. stage=market
      reason=atr_too_low
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0191. stage=market
      reason=atr_too_high
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0192. stage=market
      reason=risk_reward_too_low
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0193. stage=market
      reason=score_too_low
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0194. stage=market
      reason=adx_penalty_score_too_low
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0195. stage=market
      reason=trend_conflict_1h
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0196. stage=market
      reason=benchmark_context_conflict
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0197. stage=market
      reason=macro_risk_off_long
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0198. stage=market
      reason=pattern_missing
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0199. stage=market
      reason=threshold_too_strict
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0200. stage=market
      reason=market_condition
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0201. stage=market
      reason=source_gate
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0202. stage=market
      reason=implementation_bug
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0203. stage=market
      reason=cooldown_active
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0204. stage=market
      reason=quality_monitor_pause
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0205. stage=market
      reason=symbol_has_open_signal
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0206. stage=market
      reason=setup_has_open_signal
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0207. stage=market
      reason=notifier_disabled
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0208. stage=market
      reason=ws_cache_partial
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.

Window review pass 02
---------------------
0209. stage=data
      reason=insufficient_history
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0210. stage=data
      reason=stale_primary_frame
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0211. stage=data
      reason=stale_context_frame
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0212. stage=data
      reason=required_feature_missing
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0213. stage=data
      reason=required_enrichment_missing
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0214. stage=data
      reason=spread_unavailable
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0215. stage=data
      reason=spread_too_wide
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0216. stage=data
      reason=atr_too_low
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0217. stage=data
      reason=atr_too_high
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0218. stage=data
      reason=risk_reward_too_low
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0219. stage=data
      reason=score_too_low
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0220. stage=data
      reason=adx_penalty_score_too_low
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0221. stage=data
      reason=trend_conflict_1h
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0222. stage=data
      reason=benchmark_context_conflict
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0223. stage=data
      reason=macro_risk_off_long
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0224. stage=data
      reason=pattern_missing
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0225. stage=data
      reason=threshold_too_strict
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0226. stage=data
      reason=market_condition
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0227. stage=data
      reason=source_gate
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0228. stage=data
      reason=implementation_bug
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0229. stage=data
      reason=cooldown_active
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0230. stage=data
      reason=quality_monitor_pause
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0231. stage=data
      reason=symbol_has_open_signal
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0232. stage=data
      reason=setup_has_open_signal
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0233. stage=data
      reason=notifier_disabled
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0234. stage=data
      reason=ws_cache_partial
      action=Check REST/WS frame availability and warmup coverage.
      rule=observe first; do not tighten gates.
0235. stage=routing
      reason=insufficient_history
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0236. stage=routing
      reason=stale_primary_frame
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0237. stage=routing
      reason=stale_context_frame
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0238. stage=routing
      reason=required_feature_missing
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0239. stage=routing
      reason=required_enrichment_missing
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0240. stage=routing
      reason=spread_unavailable
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0241. stage=routing
      reason=spread_too_wide
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0242. stage=routing
      reason=atr_too_low
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0243. stage=routing
      reason=atr_too_high
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0244. stage=routing
      reason=risk_reward_too_low
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0245. stage=routing
      reason=score_too_low
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0246. stage=routing
      reason=adx_penalty_score_too_low
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0247. stage=routing
      reason=trend_conflict_1h
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0248. stage=routing
      reason=benchmark_context_conflict
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0249. stage=routing
      reason=macro_risk_off_long
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0250. stage=routing
      reason=pattern_missing
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0251. stage=routing
      reason=threshold_too_strict
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0252. stage=routing
      reason=market_condition
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0253. stage=routing
      reason=source_gate
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0254. stage=routing
      reason=implementation_bug
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0255. stage=routing
      reason=cooldown_active
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0256. stage=routing
      reason=quality_monitor_pause
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0257. stage=routing
      reason=symbol_has_open_signal
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0258. stage=routing
      reason=setup_has_open_signal
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0259. stage=routing
      reason=notifier_disabled
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0260. stage=routing
      reason=ws_cache_partial
      action=Check shortlist strategy_fits and asset-fit routing.
      rule=observe first; do not tighten gates.
0261. stage=strategy
      reason=insufficient_history
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0262. stage=strategy
      reason=stale_primary_frame
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0263. stage=strategy
      reason=stale_context_frame
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0264. stage=strategy
      reason=required_feature_missing
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0265. stage=strategy
      reason=required_enrichment_missing
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0266. stage=strategy
      reason=spread_unavailable
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0267. stage=strategy
      reason=spread_too_wide
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0268. stage=strategy
      reason=atr_too_low
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0269. stage=strategy
      reason=atr_too_high
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0270. stage=strategy
      reason=risk_reward_too_low
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0271. stage=strategy
      reason=score_too_low
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0272. stage=strategy
      reason=adx_penalty_score_too_low
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0273. stage=strategy
      reason=trend_conflict_1h
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0274. stage=strategy
      reason=benchmark_context_conflict
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0275. stage=strategy
      reason=macro_risk_off_long
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0276. stage=strategy
      reason=pattern_missing
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0277. stage=strategy
      reason=threshold_too_strict
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0278. stage=strategy
      reason=market_condition
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0279. stage=strategy
      reason=source_gate
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0280. stage=strategy
      reason=implementation_bug
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0281. stage=strategy
      reason=cooldown_active
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0282. stage=strategy
      reason=quality_monitor_pause
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0283. stage=strategy
      reason=symbol_has_open_signal
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0284. stage=strategy
      reason=setup_has_open_signal
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0285. stage=strategy
      reason=notifier_disabled
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0286. stage=strategy
      reason=ws_cache_partial
      action=Check detector pattern gates and required features.
      rule=observe first; do not tighten gates.
0287. stage=confirmation
      reason=insufficient_history
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0288. stage=confirmation
      reason=stale_primary_frame
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0289. stage=confirmation
      reason=stale_context_frame
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0290. stage=confirmation
      reason=required_feature_missing
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0291. stage=confirmation
      reason=required_enrichment_missing
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0292. stage=confirmation
      reason=spread_unavailable
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0293. stage=confirmation
      reason=spread_too_wide
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0294. stage=confirmation
      reason=atr_too_low
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0295. stage=confirmation
      reason=atr_too_high
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0296. stage=confirmation
      reason=risk_reward_too_low
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0297. stage=confirmation
      reason=score_too_low
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0298. stage=confirmation
      reason=adx_penalty_score_too_low
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0299. stage=confirmation
      reason=trend_conflict_1h
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0300. stage=confirmation
      reason=benchmark_context_conflict
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0301. stage=confirmation
      reason=macro_risk_off_long
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0302. stage=confirmation
      reason=pattern_missing
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0303. stage=confirmation
      reason=threshold_too_strict
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0304. stage=confirmation
      reason=market_condition
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0305. stage=confirmation
      reason=source_gate
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0306. stage=confirmation
      reason=implementation_bug
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0307. stage=confirmation
      reason=cooldown_active
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0308. stage=confirmation
      reason=quality_monitor_pause
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0309. stage=confirmation
      reason=symbol_has_open_signal
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0310. stage=confirmation
      reason=setup_has_open_signal
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0311. stage=confirmation
      reason=notifier_disabled
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0312. stage=confirmation
      reason=ws_cache_partial
      action=Check fast context, family gates, and flow votes.
      rule=observe first; do not tighten gates.
0313. stage=filters
      reason=insufficient_history
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0314. stage=filters
      reason=stale_primary_frame
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0315. stage=filters
      reason=stale_context_frame
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0316. stage=filters
      reason=required_feature_missing
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0317. stage=filters
      reason=required_enrichment_missing
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0318. stage=filters
      reason=spread_unavailable
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0319. stage=filters
      reason=spread_too_wide
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0320. stage=filters
      reason=atr_too_low
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0321. stage=filters
      reason=atr_too_high
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0322. stage=filters
      reason=risk_reward_too_low
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0323. stage=filters
      reason=score_too_low
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0324. stage=filters
      reason=adx_penalty_score_too_low
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0325. stage=filters
      reason=trend_conflict_1h
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0326. stage=filters
      reason=benchmark_context_conflict
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0327. stage=filters
      reason=macro_risk_off_long
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0328. stage=filters
      reason=pattern_missing
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0329. stage=filters
      reason=threshold_too_strict
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0330. stage=filters
      reason=market_condition
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0331. stage=filters
      reason=source_gate
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0332. stage=filters
      reason=implementation_bug
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0333. stage=filters
      reason=cooldown_active
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0334. stage=filters
      reason=quality_monitor_pause
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0335. stage=filters
      reason=symbol_has_open_signal
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0336. stage=filters
      reason=setup_has_open_signal
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0337. stage=filters
      reason=notifier_disabled
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0338. stage=filters
      reason=ws_cache_partial
      action=Check spread, ATR, freshness, RR, and score gates.
      rule=observe first; do not tighten gates.
0339. stage=delivery
      reason=insufficient_history
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0340. stage=delivery
      reason=stale_primary_frame
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0341. stage=delivery
      reason=stale_context_frame
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0342. stage=delivery
      reason=required_feature_missing
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0343. stage=delivery
      reason=required_enrichment_missing
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0344. stage=delivery
      reason=spread_unavailable
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0345. stage=delivery
      reason=spread_too_wide
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0346. stage=delivery
      reason=atr_too_low
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0347. stage=delivery
      reason=atr_too_high
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0348. stage=delivery
      reason=risk_reward_too_low
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0349. stage=delivery
      reason=score_too_low
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0350. stage=delivery
      reason=adx_penalty_score_too_low
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0351. stage=delivery
      reason=trend_conflict_1h
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0352. stage=delivery
      reason=benchmark_context_conflict
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0353. stage=delivery
      reason=macro_risk_off_long
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0354. stage=delivery
      reason=pattern_missing
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0355. stage=delivery
      reason=threshold_too_strict
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0356. stage=delivery
      reason=market_condition
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0357. stage=delivery
      reason=source_gate
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0358. stage=delivery
      reason=implementation_bug
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0359. stage=delivery
      reason=cooldown_active
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0360. stage=delivery
      reason=quality_monitor_pause
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0361. stage=delivery
      reason=symbol_has_open_signal
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0362. stage=delivery
      reason=setup_has_open_signal
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0363. stage=delivery
      reason=notifier_disabled
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0364. stage=delivery
      reason=ws_cache_partial
      action=Check cooldown, tracking, quality, and notifier gates.
      rule=observe first; do not tighten gates.
0365. stage=telemetry
      reason=insufficient_history
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0366. stage=telemetry
      reason=stale_primary_frame
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0367. stage=telemetry
      reason=stale_context_frame
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0368. stage=telemetry
      reason=required_feature_missing
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0369. stage=telemetry
      reason=required_enrichment_missing
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0370. stage=telemetry
      reason=spread_unavailable
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0371. stage=telemetry
      reason=spread_too_wide
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0372. stage=telemetry
      reason=atr_too_low
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0373. stage=telemetry
      reason=atr_too_high
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0374. stage=telemetry
      reason=risk_reward_too_low
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0375. stage=telemetry
      reason=score_too_low
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0376. stage=telemetry
      reason=adx_penalty_score_too_low
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0377. stage=telemetry
      reason=trend_conflict_1h
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0378. stage=telemetry
      reason=benchmark_context_conflict
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0379. stage=telemetry
      reason=macro_risk_off_long
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0380. stage=telemetry
      reason=pattern_missing
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0381. stage=telemetry
      reason=threshold_too_strict
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0382. stage=telemetry
      reason=market_condition
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0383. stage=telemetry
      reason=source_gate
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0384. stage=telemetry
      reason=implementation_bug
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0385. stage=telemetry
      reason=cooldown_active
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0386. stage=telemetry
      reason=quality_monitor_pause
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0387. stage=telemetry
      reason=symbol_has_open_signal
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0388. stage=telemetry
      reason=setup_has_open_signal
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0389. stage=telemetry
      reason=notifier_disabled
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0390. stage=telemetry
      reason=ws_cache_partial
      action=Check JSONL funnel counts and cycle summaries.
      rule=observe first; do not tighten gates.
0391. stage=market
      reason=insufficient_history
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0392. stage=market
      reason=stale_primary_frame
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0393. stage=market
      reason=stale_context_frame
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0394. stage=market
      reason=required_feature_missing
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0395. stage=market
      reason=required_enrichment_missing
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0396. stage=market
      reason=spread_unavailable
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0397. stage=market
      reason=spread_too_wide
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0398. stage=market
      reason=atr_too_low
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0399. stage=market
      reason=atr_too_high
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0400. stage=market
      reason=risk_reward_too_low
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0401. stage=market
      reason=score_too_low
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0402. stage=market
      reason=adx_penalty_score_too_low
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0403. stage=market
      reason=trend_conflict_1h
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0404. stage=market
      reason=benchmark_context_conflict
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0405. stage=market
      reason=macro_risk_off_long
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0406. stage=market
      reason=pattern_missing
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0407. stage=market
      reason=threshold_too_strict
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0408. stage=market
      reason=market_condition
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0409. stage=market
      reason=source_gate
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0410. stage=market
      reason=implementation_bug
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0411. stage=market
      reason=cooldown_active
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0412. stage=market
      reason=quality_monitor_pause
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0413. stage=market
      reason=symbol_has_open_signal
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0414. stage=market
      reason=setup_has_open_signal
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0415. stage=market
      reason=notifier_disabled
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.
0416. stage=market
      reason=ws_cache_partial
      action=Check current market condition and benchmark context.
      rule=observe first; do not tighten gates.

"""

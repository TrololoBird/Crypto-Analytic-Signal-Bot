from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from scripts.common import bootstrap_repo_path
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    from common import bootstrap_repo_path


ROOT = bootstrap_repo_path()
DEFAULT_TELEMETRY_DIR = ROOT / "data" / "bot" / "telemetry"
DEFAULT_OUTPUT = ROOT / "logs" / "telemetry_calibration_analysis.md"


@dataclass(frozen=True, slots=True)
class CalibrationIssue:
    kind: str
    subject: str
    detail: str
    severity: str = "medium"


def _tail_jsonl_lines(path: Path, *, max_lines: int, chunk_size: int = 64 * 1024) -> list[str]:
    """Read the final JSONL lines without materializing large telemetry files."""
    if max_lines <= 0 or not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        chunks: list[bytes] = []
        newline_count = 0
        while position > 0 and newline_count <= max_lines:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
        data = b"".join(reversed(chunks))
    return data.decode("utf-8", errors="ignore").splitlines()[-max_lines:]


class TelemetryAnalyzer:
    """Analyze runtime telemetry JSONL files for calibration evidence."""

    def __init__(
        self,
        telemetry_dir: Path = DEFAULT_TELEMETRY_DIR,
        *,
        run_id: str | None = None,
        max_lines_per_file: int = 500,
    ) -> None:
        self.telemetry_dir = telemetry_dir
        self.run_dir = self._resolve_run_dir(run_id)
        self.max_lines_per_file = max(1, int(max_lines_per_file))
        self.records = self._load_records()

    def analyze_strategy_performance(
        self, strategy_id: str, hours: int = 24
    ) -> dict[str, object]:
        """Return signal/reject distribution for a single strategy."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
        rows = [
            row
            for row in self.records
            if self._strategy_id(row) == strategy_id and self._row_ts(row) >= cutoff
        ]
        signal_rows = [row for row in rows if self._is_signal(row)]
        reject_reasons = Counter(str(row.get("reason") or row.get("reason_code") or "unknown") for row in rows if not self._is_signal(row))
        confidences = [
            float(row["signal_confidence"])
            for row in signal_rows
            if isinstance(row.get("signal_confidence"), int | float)
        ]
        return {
            "strategy_id": strategy_id,
            "rows": len(rows),
            "signal_count": len(signal_rows),
            "signal_rate_per_hour": round(len(signal_rows) / max(float(hours), 1.0), 6),
            "reject_breakdown": dict(reject_reasons.most_common()),
            "top_reject_reason": reject_reasons.most_common(1)[0][0] if reject_reasons else "",
            "avg_confidence": round(sum(confidences) / len(confidences), 6) if confidences else None,
            "symbol_distribution": dict(Counter(str(row.get("symbol") or "unknown") for row in signal_rows).most_common()),
            "timeframe_distribution": dict(Counter(str(row.get("timeframe") or row.get("event_interval") or "unknown") for row in signal_rows).most_common()),
        }

    def detect_calibration_issues(self) -> list[CalibrationIssue]:
        """Detect calibration issues from loaded strategy/cycle telemetry."""
        issues: list[CalibrationIssue] = []
        by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_symbol: Counter[str] = Counter()
        stop_anchor_rejects: Counter[str] = Counter()
        priority_signal_counts: Counter[str] = Counter()

        for row in self.records:
            strategy_id = str(row.get("strategy_id") or row.get("setup_id") or "")
            symbol = str(row.get("symbol") or row.get("event_symbol") or "")
            if strategy_id:
                by_strategy[strategy_id].append(row)
            if symbol:
                by_symbol[symbol] += 1
            reason = str(row.get("reason") or row.get("reason_code") or "")
            if "external_swing_stop_missing" in reason or "swing_stop_missing" in reason:
                stop_anchor_rejects[reason] += 1
            if symbol in {"BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT"} and self._is_signal(row):
                priority_signal_counts[symbol] += 1

        for strategy_id, rows in sorted(by_strategy.items()):
            signals = sum(1 for row in rows if self._is_signal(row))
            rejects = max(len(rows) - signals, 0)
            if signals == 0 and len(rows) > 0:
                issues.append(
                    CalibrationIssue(
                        kind="zero_signal_strategy",
                        subject=strategy_id,
                        detail=f"{len(rows)} decisions, 0 signals",
                    )
                )
            if len(rows) >= 20 and rejects / len(rows) > 0.80:
                issues.append(
                    CalibrationIssue(
                        kind="high_reject_rate",
                        subject=strategy_id,
                        detail=f"{rejects}/{len(rows)} rejects",
                    )
                )

        long_missing = stop_anchor_rejects.get("external_swing_stop_missing_long", 0) + stop_anchor_rejects.get("swing_stop_missing_long", 0)
        short_missing = stop_anchor_rejects.get("external_swing_stop_missing_short", 0) + stop_anchor_rejects.get("swing_stop_missing_short", 0)
        if min(long_missing, short_missing) > 0 and max(long_missing, short_missing) / max(min(long_missing, short_missing), 1) > 10:
            issues.append(
                CalibrationIssue(
                    kind="bos_choch_stop_anchor_imbalance",
                    subject="bos_choch",
                    detail=f"long={long_missing}, short={short_missing}",
                    severity="high",
                )
            )

        for symbol in ("BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT"):
            if by_symbol[symbol] == 0:
                issues.append(
                    CalibrationIssue(
                        kind="priority_asset_no_coverage",
                        subject=symbol,
                        detail="no telemetry rows for priority asset",
                        severity="high",
                    )
                )
            elif priority_signal_counts[symbol] == 0:
                issues.append(
                    CalibrationIssue(
                        kind="priority_asset_zero_signals",
                        subject=symbol,
                        detail=f"{by_symbol[symbol]} rows, 0 signals",
                    )
                )

        return issues

    def recommend_threshold_adjustments(self) -> list[dict[str, object]]:
        """Suggest conservative threshold review targets from reject distributions."""
        recommendations: list[dict[str, object]] = []
        for strategy_id in sorted({self._strategy_id(row) for row in self.records}):
            if not strategy_id:
                continue
            perf = self.analyze_strategy_performance(strategy_id)
            top_reason = str(perf.get("top_reject_reason") or "")
            rows = int(perf.get("rows") or 0)
            signals = int(perf.get("signal_count") or 0)
            if rows >= 20 and signals == 0 and top_reason:
                recommendations.append(
                    {
                        "strategy_id": strategy_id,
                        "target": top_reason,
                        "action": "review_threshold_with_backtest",
                        "evidence_rows": rows,
                    }
                )
        return recommendations

    def write_report(self, output: Path, *, hours: int = 24) -> Path:
        """Write a concise markdown report plus adjacent JSON payload."""
        output.parent.mkdir(parents=True, exist_ok=True)
        strategy_ids = sorted({self._strategy_id(row) for row in self.records if self._strategy_id(row)})
        performances = [self.analyze_strategy_performance(strategy_id, hours=hours) for strategy_id in strategy_ids]
        issues = self.detect_calibration_issues()
        recommendations = self.recommend_threshold_adjustments()
        lines = [
            "# Telemetry Calibration Analysis",
            "",
            f"Run: `{self.run_dir.name}`",
            f"Records: {len(self.records)}",
            f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "",
            "## Strategy Performance",
            "",
            "| Strategy | Rows | Signals | Signal/hour | Top Reject |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for row in performances:
            lines.append(
                f"| `{row['strategy_id']}` | {row['rows']} | {row['signal_count']} | {row['signal_rate_per_hour']} | `{row['top_reject_reason']}` |"
            )
        lines.extend(["", "## Calibration Issues", ""])
        if issues:
            for issue in issues:
                lines.append(f"- `{issue.severity}` `{issue.kind}` `{issue.subject}`: {issue.detail}")
        else:
            lines.append("- None detected from loaded telemetry.")
        lines.extend(["", "## Threshold Review Targets", ""])
        if recommendations:
            for item in recommendations:
                lines.append(
                    f"- `{item['strategy_id']}`: `{item['target']}` ({item['evidence_rows']} rows)"
                )
        else:
            lines.append("- None.")
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        output.with_suffix(".json").write_text(
            json.dumps(
                {
                    "run_id": self.run_dir.name,
                    "record_count": len(self.records),
                    "strategy_performance": performances,
                    "calibration_issues": [asdict(issue) for issue in issues],
                    "threshold_recommendations": recommendations,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return output

    def _resolve_run_dir(self, run_id: str | None) -> Path:
        runs_dir = self.telemetry_dir / "runs"
        if run_id:
            return runs_dir / run_id
        candidates = [path for path in runs_dir.glob("*") if path.is_dir()] if runs_dir.exists() else []
        if not candidates:
            return runs_dir / "missing"
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _load_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.run_dir.exists():
            return records
        for path in (self.run_dir / "analysis").rglob("*.jsonl"):
            for line in _tail_jsonl_lines(path, max_lines=self.max_lines_per_file):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    records.append(row)
        return records

    @staticmethod
    def _row_ts(row: Mapping[str, Any]) -> datetime:
        raw = row.get("ts") or row.get("timestamp") or row.get("health_monitor_ts")
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return datetime.min.replace(tzinfo=timezone.utc)
        raw_ms = row.get("timestamp_ms")
        if isinstance(raw_ms, int | float):
            return datetime.fromtimestamp(float(raw_ms) / 1000.0, tz=timezone.utc)
        return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _strategy_id(row: Mapping[str, Any]) -> str:
        return str(row.get("strategy_id") or row.get("setup_id") or "")

    @staticmethod
    def _is_signal(row: Mapping[str, Any]) -> bool:
        if row.get("decision") == "signal":
            return True
        if row.get("signal_emitted") is True:
            return True
        if isinstance(row.get("signal_count"), int | float) and float(row["signal_count"]) > 0:
            return True
        if isinstance(row.get("signals_found"), int | float) and float(row["signals_found"]) > 0:
            return True
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze runtime telemetry for calibration issues.")
    parser.add_argument("--telemetry-dir", type=Path, default=DEFAULT_TELEMETRY_DIR)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-lines-per-file", type=int, default=500)
    args = parser.parse_args()

    analyzer = TelemetryAnalyzer(
        args.telemetry_dir,
        run_id=args.run_id or None,
        max_lines_per_file=args.max_lines_per_file,
    )
    output = analyzer.write_report(args.output, hours=max(1, int(args.hours)))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

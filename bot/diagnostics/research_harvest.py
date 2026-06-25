"""JSONL recorder for research harvest sessions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from engine.domain.schemas import PipelineResult, PreparedSymbol


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _frame_tail(frame: Any, *, max_rows: int) -> dict[str, Any] | None:
    if frame is None or max_rows <= 0:
        return None
    is_empty = getattr(frame, "is_empty", None)
    if callable(is_empty) and is_empty():
        return None
    try:
        height = int(frame.height)
    except (AttributeError, TypeError, ValueError):
        return None
    if height <= 0:
        return None
    tail = frame.tail(max_rows)
    try:
        rows = tail.to_dicts()
    except (AttributeError, TypeError, ValueError):
        return {"row_count": height}
    return {"row_count": height, "tail": _json_safe(rows)}


def prepared_snapshot(
    prepared: PreparedSymbol | None,
    *,
    indicator_snapshot: dict[str, dict[str, float]] | None = None,
    ws_enrichments: dict[str, Any] | None = None,
    max_bar_tail: int = 0,
) -> dict[str, Any] | None:
    if prepared is None:
        return None
    universe = prepared.universe
    row: dict[str, Any] = {
        "symbol": prepared.symbol,
        "primary_timeframe": prepared.primary_timeframe,
        "bias_4h": prepared.bias_4h,
        "bias_1h": prepared.bias_1h,
        "structure_1h": prepared.structure_1h,
        "regime_4h_confirmed": prepared.regime_4h_confirmed,
        "regime_1h_confirmed": prepared.regime_1h_confirmed,
        "market_regime": prepared.market_regime,
        "global_market_regime": prepared.global_market_regime,
        "btc_phase": prepared.btc_phase,
        "spread_bps": prepared.spread_bps,
        "atr_pct": prepared.atr_pct,
        "mark_price": prepared.mark_price,
        "funding_rate": prepared.funding_rate,
        "oi_change_pct": prepared.oi_change_pct,
        "ls_ratio": prepared.ls_ratio,
        "depth_imbalance": prepared.depth_imbalance,
        "liquidation_score": prepared.liquidation_score,
        "spot_lead_return_1m": prepared.spot_lead_return_1m,
        "spot_futures_spread_bps": prepared.spot_futures_spread_bps,
        "data_freshness_flags": list(prepared.data_freshness_flags),
        "data_quality_flags": list(prepared.data_quality_flags),
        "degraded": prepared.degraded,
        "degrade_reason": prepared.degrade_reason,
        "quote_volume": getattr(universe, "quote_volume", None),
        "price_change_pct": getattr(universe, "price_change_pct", None),
    }
    if indicator_snapshot:
        row["indicators"] = indicator_snapshot
    if ws_enrichments:
        row["ws_enrichments"] = _json_safe(ws_enrichments)
    if max_bar_tail > 0:
        row["bars"] = {
            "5m": _frame_tail(prepared.work_5m, max_rows=max_bar_tail),
            "15m": _frame_tail(prepared.work_15m, max_rows=max_bar_tail),
            "1h": _frame_tail(prepared.work_1h, max_rows=max_bar_tail),
            "4h": _frame_tail(prepared.work_4h, max_rows=max_bar_tail),
        }
    return row


class ResearchHarvestRecorder:
    """Append-only JSONL writer under ``data/research_harvest/{run_id}/``."""

    def __init__(
        self,
        *,
        root_dir: Path,
        run_id: str,
        symbols: tuple[str, ...],
        config_path: Path,
    ) -> None:
        self.run_id = run_id
        self.symbols = symbols
        self.session_dir = root_dir / run_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._cycles_path = self.session_dir / "cycles.jsonl"
        self._manifest_path = self.session_dir / "manifest.json"
        self._started_at = datetime.now(UTC)
        self._cycle_count = 0
        self._manifest_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "started_at": self._started_at.isoformat(),
                    "symbols": list(symbols),
                    "config_path": str(config_path),
                    "mode": "research_harvest",
                    "note": "Calibration deferred - capture only; no Telegram delivery.",
                },
                ensure_ascii=True,
                indent=2,
            ),
            encoding="utf-8",
        )

    def record_cycle(
        self,
        *,
        symbol: str,
        interval: str,
        event_ts: datetime,
        result: PipelineResult,
        candidates: list[Any],
        rejected: list[dict[str, Any]],
        prepared_snapshot_row: dict[str, Any] | None,
    ) -> None:
        self._cycle_count += 1
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "symbol": symbol,
            "interval": interval,
            "event_ts": event_ts.isoformat(),
            "trigger": result.trigger,
            "status": result.status,
            "raw_setups": result.raw_setups,
            "candidate_count": len(candidates),
            "rejected_count": len(rejected),
            "funnel": _json_safe(result.funnel) if result.funnel else {},
            "prepared": prepared_snapshot_row,
            "candidates": [
                {
                    "setup_id": getattr(item, "setup_id", None),
                    "direction": getattr(item, "direction", None),
                    "score": round(float(getattr(item, "score", 0.0) or 0.0), 6),
                    "timeframe": getattr(item, "timeframe", None),
                }
                for item in candidates
            ],
            "rejected_sample": rejected[:40],
        }
        self._append_jsonl(self._cycles_path, row)
        symbol_dir = self.session_dir / "symbols" / symbol
        symbol_dir.mkdir(parents=True, exist_ok=True)
        self._append_jsonl(symbol_dir / "cycles.jsonl", row)

    def finalize(self, *, extra: dict[str, Any] | None = None) -> Path:
        finished = datetime.now(UTC)
        payload = {
            "run_id": self.run_id,
            "started_at": self._started_at.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": (finished - self._started_at).total_seconds(),
            "cycle_records": self._cycle_count,
            "symbols": list(self.symbols),
            "session_dir": str(self.session_dir),
        }
        if extra:
            payload.update(extra)
        self._manifest_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return self.session_dir

    @staticmethod
    def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=True, default=str))
            handle.write("\n")

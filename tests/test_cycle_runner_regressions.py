from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from bot.application.cycle_runner import CycleRunner
from bot.domain.schemas import PipelineResult, Signal, UniverseSymbol


class _TrackerStub:
    async def review_open_signals(self, *, dry_run: bool) -> list[Any]:
        return []


class _TelemetryStub:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def append_jsonl(self, filename: str, row: dict[str, Any]) -> None:
        self.rows.append((filename, row))


class _OIRefreshStub:
    async def refresh_once(self, *_args: Any, **_kwargs: Any) -> int:
        return 0


class _BotStub:
    def __init__(self) -> None:
        self.tracker = _TrackerStub()
        self.telemetry = _TelemetryStub()
        self._shortlist_lock = asyncio.Lock()
        self._shortlist = [
            UniverseSymbol(
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                contract_type="PERPETUAL",
                status="TRADING",
                onboard_date_ms=0,
                quote_volume=1_000_000.0,
                price_change_pct=0.0,
                last_price=100.0,
            )
        ]
        self._analysis_semaphore = asyncio.Semaphore(1)
        self.settings = SimpleNamespace(
            runtime=SimpleNamespace(
                emergency_context_warmup_timeout_seconds=1.0,
                emergency_context_warmup_symbol_limit=1,
                emergency_context_fetch_timeout_seconds=1.0,
                max_signals_per_cycle=5,
            )
        )
        self.last_cycle_summary: dict[str, Any] = {}
        self.candidate = Signal(
            symbol="BTCUSDT",
            setup_id="ema_bounce",
            direction="long",
            score=0.7,
            timeframe="15m",
            entry_low=100.0,
            entry_high=100.0,
            stop=98.0,
            take_profit_1=103.0,
            take_profit_2=105.0,
        )

    def _get_oi_refresh_runner(self) -> _OIRefreshStub:
        return _OIRefreshStub()

    async def _fetch_frames(self, _item: UniverseSymbol) -> object:
        return object()

    def _ws_cache_enrichments(self, _symbol: str) -> dict[str, Any]:
        return {}

    async def _run_modern_analysis(
        self, item: UniverseSymbol, *_args: Any, **_kwargs: Any
    ) -> PipelineResult:
        return PipelineResult(
            symbol=item.symbol,
            trigger="emergency_fallback",
            event_ts=datetime.now(UTC),
            raw_setups=1,
            candidates=[self.candidate],
            prepared=SimpleNamespace(bias_4h="neutral"),
        )

    def _select_and_rank(
        self, all_candidates: dict[str, list[Signal]], *, max_signals: int
    ) -> list[Signal]:
        return [signal for signals in all_candidates.values() for signal in signals][:max_signals]

    async def _select_and_deliver(
        self,
        selected: list[Signal],
        *,
        prepared_by_tracking_id: dict[str, Any],
    ) -> tuple[list[Signal], list[dict[str, Any]], Counter[str]]:
        return [], [], Counter({"suppressed": len(selected)})

    def _emit_cycle_log(self, **_kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_emergency_cycle_summary_separates_selected_from_delivered() -> None:
    summary = await CycleRunner(_BotStub()).run_emergency_cycle()

    assert summary["post_filter_candidates"] == 1
    assert summary["selected_signals"] == 1
    assert summary["delivered_signals"] == 0
    assert summary["selected"] == 1
    assert summary["delivered"] == 0
    assert summary["delivery_status_counts"] == {"suppressed": 1}
